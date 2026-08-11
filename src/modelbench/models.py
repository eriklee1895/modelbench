"""Core data models for the benchmark.

A RunResult is the atomic unit: one streaming Responses-API call, fully timed.
Everything downstream (aggregation, report) consumes RunResult rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TokenUsage:
    """Token counts, preferring exact API usage; est_* are tiktoken fallbacks."""

    input_tokens: int | None = None
    output_tokens: int | None = None  # total output (content + reasoning)
    content_tokens: int | None = None  # output minus reasoning (the visible answer)
    cached_tokens: int | None = None
    source: str = "api"  # "api" | "est"


@dataclass
class RunResult:
    # identity
    endpoint: str
    group: str  # official | volc
    vendor: str
    model: str
    case: str  # S | M | L | XL | agent | cache
    rep: int  # repeat index (0-based); for cache case this is the hit-attempt index
    effort: str | None = None  # reasoning effort tier (low/medium/high); None = default

    # timing (seconds, perf_counter deltas)
    ttft_s: float | None = None  # -> first output_text delta (may include reasoning)
    ttft_content_s: float | None = None  # -> first non-reasoning content token
    reasoning_ttft_s: float | None = None  # -> first reasoning delta (if reasoning model)
    e2e_s: float | None = None  # request start -> response.completed / stream end

    # throughput (derived; None until computed)
    output_tps: float | None = None  # output_tokens / (e2e - ttft)
    e2e_tps: float | None = None  # output_tokens / e2e
    # chunk-timing decode rate: tokens after the first content chunk divided by
    # the observed inter-chunk window. The first chunk is excluded because its
    # generation happened before the first client-observed timestamp.
    decode_tps: float | None = None
    decode_window_s: float | None = None  # last - first content delta
    content_chunk_tokens: int | None = None  # estimated tokens over content text
    first_content_chunk_tokens: int | None = None

    usage: TokenUsage = field(default_factory=TokenUsage)

    # agent-case specific
    tool_call_ttft_s: float | None = None  # -> first function_call item
    tool_call_valid: bool | None = None  # schema-parseable function_call emitted

    # outcome
    success: bool = False
    finish_reason: str | None = None
    error_type: str | None = None  # rate_limit | timeout | server | empty | invalid_tool | other
    error_detail: str | None = None
    retried: bool = False

    # raw text (kept small; used for tool validation & sanity)
    output_text: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def derive(self) -> None:
        """Compute derived throughput metrics in-place.

        TPS = content tokens / content-decode window. For reasoning models the
        window starts at the first content token (excludes thinking); for plain
        models it starts at TTFT. Falls back to total output tokens when the
        backend doesn't split reasoning.
        """
        if self.e2e_s is None:
            return
        num = self.usage.content_tokens or self.usage.output_tokens
        # decode window start: content start if we have it, else first-byte
        win_start = self.ttft_content_s if self.ttft_content_s is not None else self.ttft_s
        if num and win_start is not None:
            decode = self.e2e_s - win_start
            if decode > 0:
                self.output_tps = num / decode
            if self.e2e_s > 0:
                self.e2e_tps = num / self.e2e_s

    def to_row(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "group": self.group,
            "vendor": self.vendor,
            "model": self.model,
            "case": self.case,
            "rep": self.rep,
            "effort": self.effort,
            "ttft_s": self.ttft_s,
            "ttft_content_s": self.ttft_content_s,
            "reasoning_ttft_s": self.reasoning_ttft_s,
            "e2e_s": self.e2e_s,
            "output_tps": self.output_tps,
            "e2e_tps": self.e2e_tps,
            "decode_tps": self.decode_tps,
            "decode_window_s": self.decode_window_s,
            "content_chunk_tokens": self.content_chunk_tokens,
            "first_content_chunk_tokens": self.first_content_chunk_tokens,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "content_tokens": self.usage.content_tokens,
            "cached_tokens": self.usage.cached_tokens,
            "usage_source": self.usage.source,
            "tool_call_ttft_s": self.tool_call_ttft_s,
            "tool_call_valid": self.tool_call_valid,
            "success": self.success,
            "finish_reason": self.finish_reason,
            "error_type": self.error_type,
            "error_detail": self.error_detail,
            "retried": self.retried,
            "output_text": self.output_text[:500],  # truncate for storage
            "tool_calls": self.tool_calls,
            "ts": time.time(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> RunResult:
        r = cls(
            endpoint=row["endpoint"],
            group=row["group"],
            vendor=row["vendor"],
            model=row["model"],
            case=row["case"],
            rep=row["rep"],
            effort=row.get("effort"),
        )
        r.ttft_s = row.get("ttft_s")
        r.ttft_content_s = row.get("ttft_content_s")
        r.reasoning_ttft_s = row.get("reasoning_ttft_s")
        r.e2e_s = row.get("e2e_s")
        r.output_tps = row.get("output_tps")
        r.e2e_tps = row.get("e2e_tps")
        r.decode_tps = row.get("decode_tps")
        r.decode_window_s = row.get("decode_window_s")
        r.content_chunk_tokens = row.get("content_chunk_tokens")
        r.first_content_chunk_tokens = row.get("first_content_chunk_tokens")
        r.usage = TokenUsage(
            input_tokens=row.get("input_tokens"),
            output_tokens=row.get("output_tokens"),
            content_tokens=row.get("content_tokens"),
            cached_tokens=row.get("cached_tokens"),
            source=row.get("usage_source", "api"),
        )
        r.tool_call_ttft_s = row.get("tool_call_ttft_s")
        r.tool_call_valid = row.get("tool_call_valid")
        r.success = row.get("success", False)
        r.finish_reason = row.get("finish_reason")
        r.error_type = row.get("error_type")
        r.error_detail = row.get("error_detail")
        r.retried = row.get("retried", False)
        r.output_text = row.get("output_text", "")
        r.tool_calls = row.get("tool_calls", [])
        return r
