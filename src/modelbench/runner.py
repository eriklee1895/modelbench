"""Streaming runner: one Responses-API call, precisely timed.

TTFT  = time to first `response.output_text.delta`
E2E   = request start -> `response.completed` (or stream end)
TPS   = output_tokens / (E2E - TTFT)   (pure decode rate)

Reasoning models emit reasoning deltas before text; we timestamp the first
reasoning delta and the first *content* text delta separately so the report can
distinguish "time to first byte" from "time to first real answer token".
"""

from __future__ import annotations

import asyncio
import json
import time

from openai import AsyncOpenAI

from .config import Defaults, Endpoint
from .models import RunResult, TokenUsage
from .workloads import Workload


def _classify_error(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "ratelimit" in name or "rate limit" in msg or "429" in msg:
        return "rate_limit"
    if "timeout" in name or "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "internal" in msg or "500" in msg or "502" in msg or "503" in msg or "server" in name:
        return "server"
    return "other"


def _extract_usage(final: object) -> TokenUsage:
    """Pull token counts from response.completed usage; mark source=api."""
    u = TokenUsage(source="api")
    usage = getattr(final, "usage", None)
    if usage is None and isinstance(final, dict):
        usage = final.get("usage")
    if usage is None:
        u.source = "est"
        return u
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    u.input_tokens = get("input_tokens") or get("prompt_tokens")
    u.output_tokens = get("output_tokens") or get("completion_tokens")
    # cached tokens live under input_tokens_details.cached_tokens (Responses) /
    # prompt_tokens_details.cached_tokens (chat) depending on backend.
    det = get("input_tokens_details") or get("prompt_tokens_details")
    if det is not None:
        dget = det.get if isinstance(det, dict) else lambda k, d=None: getattr(det, k, d)
        u.cached_tokens = dget("cached_tokens")
    # reasoning tokens -> content = output - reasoning (the visible answer)
    odet = get("output_tokens_details") or get("completion_tokens_details")
    reasoning = None
    if odet is not None:
        oget = odet.get if isinstance(odet, dict) else lambda k, d=None: getattr(odet, k, d)
        reasoning = oget("reasoning_tokens")
    if u.output_tokens is not None:
        u.content_tokens = u.output_tokens - (reasoning or 0)
    if u.input_tokens is None and u.output_tokens is None:
        u.source = "est"
    return u


def _est_tokens(workload: Workload, output_text: str) -> TokenUsage:
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    return TokenUsage(
        input_tokens=len(enc.encode(workload.input_text + workload.instructions)),
        output_tokens=len(enc.encode(output_text)) if output_text else 0,
        cached_tokens=None,
        source="est",
    )


def _getter(obj: object):
    """Attr/key accessor that works for both pydantic objects and dicts."""
    if isinstance(obj, dict):
        return obj.get
    return lambda k, d=None: getattr(obj, k, d)


async def run_once(
    endpoint: Endpoint,
    workload: Workload,
    defaults: Defaults,
    rep: int,
) -> RunResult:
    r = RunResult(
        endpoint=endpoint.name,
        group=endpoint.group,
        vendor=endpoint.vendor,
        model="",  # set by caller via workload_model
        case=workload.case,
        rep=rep,
        effort=workload.effort,
    )
    model = getattr(workload, "model", None) or ""
    r.model = model

    client = AsyncOpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        timeout=defaults.timeout_s,
        max_retries=0,  # we handle retry ourselves so we can record it
    )

    # Reasoning models burn a large, load-independent share of the output budget on
    # thinking (doubao-seed-2.1-turbo peaks >8k tokens even on tiny prompts). Give
    # generous headroom so the visible answer is non-empty and decode TPS is
    # measurable. Content TPS is reported separately from reasoning, so this doesn't
    # pollute the content-TPS metric.
    content_budget = min(workload.max_output_tokens, defaults.max_output_tokens_cap)
    max_out = min(content_budget + 16384, defaults.max_output_tokens_cap)

    kwargs: dict = {
        "model": model,
        "input": workload.input_text + workload.instructions,
        "max_output_tokens": max_out,
        "stream": True,
    }
    if defaults.temperature is not None:
        kwargs["temperature"] = defaults.temperature
    if workload.tools:
        kwargs["tools"] = workload.tools
    if workload.tool_choice:
        # DeepSeek's official thinking mode rejects forced tool_choice; "auto" still
        # elicits a function_call for our agent prompt, so degrade gracefully there.
        tc = workload.tool_choice
        if tc == "required" and endpoint.name == "deepseek-official":
            tc = "auto"
        kwargs["tool_choice"] = tc
    if workload.effort:
        kwargs["reasoning"] = {"effort": workload.effort}

    start = time.perf_counter()
    text_parts: list[str] = []
    saw_reasoning = False
    tool_call_seen = False
    fc_arg_parts: dict[str, list[str]] = {}  # item_id -> argument delta fragments
    fc_names: dict[str, str] = {}  # item_id -> function name
    first_content_ts: float | None = None  # first output_text delta (decode window start)
    last_content_ts: float | None = None  # last output_text delta (decode window end)
    final_resp: object | None = None
    finish: str | None = None

    async def _attempt() -> None:
        nonlocal final_resp, finish, saw_reasoning, tool_call_seen, first_content_ts, last_content_ts
        stream = await client.responses.create(**kwargs)
        async for event in stream:
            etype = getattr(event, "type", None)
            now = time.perf_counter()
            if etype == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if saw_reasoning:
                    # reasoning model: content tokens are the real "answer" stream
                    if r.ttft_content_s is None and delta:
                        r.ttft_content_s = now - start
                elif r.ttft_s is None:
                    r.ttft_s = now - start
                if delta:
                    text_parts.append(delta)
                    if first_content_ts is None:
                        first_content_ts = now
                    last_content_ts = now
            elif etype and etype.startswith("response.reasoning"):
                if not saw_reasoning:
                    saw_reasoning = True
                    r.reasoning_ttft_s = now - start
            elif etype == "response.output_item.added":
                item = getattr(event, "item", None)
                itype = getattr(item, "type", None) if item is not None else None
                if itype in ("function_call", "tool_call"):
                    iid = getattr(item, "id", None) or ""
                    fc_names[iid] = getattr(item, "name", None) or ""
                    fc_arg_parts.setdefault(iid, [])
                    if not tool_call_seen:
                        tool_call_seen = True
                        r.tool_call_ttft_s = now - start
            elif etype == "response.function_call_arguments.delta":
                iid = getattr(event, "item_id", "") or ""
                fc_arg_parts.setdefault(iid, []).append(getattr(event, "delta", "") or "")
            elif etype == "response.function_call_arguments.done":
                # authoritative full arguments for this item
                iid = getattr(event, "item_id", "") or ""
                fc_arg_parts[iid] = [getattr(event, "arguments", "") or ""]
            elif etype == "response.completed":
                final_resp = getattr(event, "response", None)
                r.e2e_s = now - start
            elif etype in ("response.incomplete", "response.failed"):
                final_resp = getattr(event, "response", None)
                r.e2e_s = now - start

    try:
        await _attempt()
    except Exception as exc:
        etype = _classify_error(exc)
        if etype in ("rate_limit", "timeout", "server"):
            r.retried = True
            # reset ALL per-attempt state so the failed attempt doesn't leak in
            text_parts.clear()
            fc_arg_parts.clear()
            fc_names.clear()
            saw_reasoning = False
            tool_call_seen = False
            final_resp = None
            r.ttft_s = r.ttft_content_s = r.reasoning_ttft_s = None
            r.tool_call_ttft_s = None
            start = time.perf_counter()
            try:
                await asyncio.sleep(1.0)
                await _attempt()
            except Exception as exc2:
                r.success = False
                r.error_type = _classify_error(exc2)
                r.error_detail = str(exc2)[:300]
                r.e2e_s = time.perf_counter() - start
                r.derive()
                return r
        else:
            r.success = False
            r.error_type = etype
            r.error_detail = str(exc)[:300]
            r.e2e_s = time.perf_counter() - start
            r.derive()
            return r

    if r.e2e_s is None:
        r.e2e_s = time.perf_counter() - start

    # Assemble tool calls from accumulated argument fragments.
    for iid, parts in fc_arg_parts.items():
        r.tool_calls.append({"name": fc_names.get(iid, ""), "arguments": "".join(parts)})
    # Fallback: read function_call items straight from the completed response.
    if not r.tool_calls and final_resp is not None:
        fget = _getter(final_resp)
        for item in fget("output", []) or []:
            iget = _getter(item)
            if iget("type") == "function_call":
                r.tool_calls.append({"name": iget("name"), "arguments": iget("arguments")})
                if r.tool_call_ttft_s is None:
                    r.tool_call_ttft_s = r.ttft_s

    r.output_text = "".join(text_parts)

    # Authoritative decode TPS from chunk timing: independent of whether the backend
    # returns usage, and immune to thinking-length variance (window starts at the
    # first content chunk). Token count prefers API content_tokens; falls back to
    # tiktoken over the content text.
    if first_content_ts is not None and last_content_ts is not None:
        window = last_content_ts - first_content_ts
        r.decode_window_s = window if window > 0 else None

    usage = _extract_usage(final_resp) if final_resp is not None else TokenUsage(source="est")
    if usage.source == "est" or usage.output_tokens is None:
        est = _est_tokens(workload, r.output_text)
        usage.input_tokens = usage.input_tokens if usage.input_tokens is not None else est.input_tokens
        usage.output_tokens = usage.output_tokens if usage.output_tokens is not None else est.output_tokens
        if usage.input_tokens is None and usage.output_tokens is None:
            usage.source = "est"
    r.usage = usage

    # decode_tps: prefer API content_tokens; else tiktoken over content text.
    if r.decode_window_s and r.decode_window_s > 0:
        import tiktoken

        ct = usage.content_tokens
        if ct is None:
            ct = len(tiktoken.get_encoding("cl100k_base").encode(r.output_text)) if r.output_text else 0
        r.content_chunk_tokens = ct
        if ct > 0:
            r.decode_tps = ct / r.decode_window_s

    # finish reason
    if final_resp is not None:
        fr = getattr(final_resp, "status", None) or (
            final_resp.get("status") if isinstance(final_resp, dict) else None
        )
        finish = fr
        # incomplete_details.reason == max_output_tokens indicates truncation
        det = getattr(final_resp, "incomplete_details", None) or (
            final_resp.get("incomplete_details") if isinstance(final_resp, dict) else None
        )
        if det:
            reason = getattr(det, "reason", None) or (det.get("reason") if isinstance(det, dict) else None)
            if reason:
                finish = f"{fr}:{reason}"
    r.finish_reason = finish

    # tool_call validity (agent case): at least one function_call with parseable args
    if workload.case == "agent":
        valid = False
        for tc in r.tool_calls:
            args = tc.get("arguments")
            if args is None:
                continue
            try:
                parsed = json.loads(args) if isinstance(args, str) else args
                if isinstance(parsed, dict) and "query" in parsed:
                    valid = True
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        r.tool_call_valid = valid
        if not valid:
            r.success = False
            r.error_type = "invalid_tool"
            r.derive()
            return r

    # success determination
    if r.output_text or r.tool_calls:
        if r.usage.output_tokens == 0 and not r.tool_calls:
            r.success = False
            r.error_type = "empty"
        else:
            r.success = True
    else:
        r.success = False
        if r.error_type is None:
            r.error_type = "empty"

    r.derive()
    return r
