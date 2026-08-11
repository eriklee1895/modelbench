"""Aggregation: turn RunResult rows into per-(endpoint,model,case) stats."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .models import RunResult


@dataclass
class Stats:
    endpoint: str
    group: str
    vendor: str
    model: str
    case: str
    n: int
    n_success: int
    success_rate: float
    # latency
    ttft_p50: float | None
    ttft_p95: float | None
    ttft_content_p50: float | None
    reasoning_ttft_p50: float | None
    is_reasoning: bool
    e2e_p50: float | None
    e2e_p95: float | None
    # throughput
    tps_p50: float | None
    tps_p95: float | None
    tps_std: float | None
    e2e_tps_p50: float | None
    decode_tps_p50: float | None  # client-observed chunk-timing rate
    decode_tps_p95: float | None
    # tokens
    out_tokens_p50: float | None
    usage_source: str
    # agent
    tool_ttft_p50: float | None
    tool_valid_rate: float | None
    # cache
    cached_tokens_p50: float | None


def _p50(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(0.95 * (len(xs) - 1))))
    return xs[k]


def _std(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) if len(xs) > 1 else (0.0 if xs else None)


def aggregate(results: list[RunResult]) -> list[Stats]:
    groups: dict[tuple, list[RunResult]] = {}
    for r in results:
        if r.rep < 0:  # warmup
            continue
        groups.setdefault((r.endpoint, r.group, r.vendor, r.model, r.case), []).append(r)

    out: list[Stats] = []
    for (endpoint, group, vendor, model, case), rs in sorted(groups.items()):
        ok = [r for r in rs if r.success]
        metric_ok = [r for r in ok if not r.retried] or ok
        ttfts = [r.ttft_s for r in metric_ok if r.ttft_s is not None]
        ttftc = [r.ttft_content_s for r in metric_ok if r.ttft_content_s is not None]
        reas = [r.reasoning_ttft_s for r in metric_ok if r.reasoning_ttft_s is not None]
        is_reasoning = len(reas) > 0
        e2es = [r.e2e_s for r in metric_ok if r.e2e_s is not None]
        tps = [r.output_tps for r in metric_ok if r.output_tps is not None]
        e2e_tps = [r.e2e_tps for r in metric_ok if r.e2e_tps is not None]
        decode = [r.decode_tps for r in metric_ok if r.decode_tps is not None]
        outtok = [float(r.usage.output_tokens) for r in metric_ok if r.usage.output_tokens is not None]
        cached = [float(r.usage.cached_tokens) for r in metric_ok if r.usage.cached_tokens is not None]
        tool_ttft = [r.tool_call_ttft_s for r in metric_ok if r.tool_call_ttft_s is not None]
        tool_valid = [r.tool_call_valid for r in rs if r.tool_call_valid is not None]
        src = "api" if any(r.usage.source == "api" for r in ok) else "est"
        out.append(
            Stats(
                endpoint=endpoint,
                group=group,
                vendor=vendor,
                model=model,
                case=case,
                n=len(rs),
                n_success=len(ok),
                success_rate=len(ok) / len(rs) if rs else 0.0,
                ttft_p50=_p50(ttfts),
                ttft_p95=_p95(ttfts),
                ttft_content_p50=_p50(ttftc),
                reasoning_ttft_p50=_p50(reas),
                is_reasoning=is_reasoning,
                e2e_p50=_p50(e2es),
                e2e_p95=_p95(e2es),
                tps_p50=_p50(tps),
                tps_p95=_p95(tps),
                tps_std=_std(tps),
                e2e_tps_p50=_p50(e2e_tps),
                decode_tps_p50=_p50(decode),
                decode_tps_p95=_p95(decode),
                out_tokens_p50=_p50(outtok),
                usage_source=src,
                tool_ttft_p50=_p50(tool_ttft),
                tool_valid_rate=(sum(1 for v in tool_valid if v) / len(tool_valid)) if tool_valid else None,
                cached_tokens_p50=_p50(cached),
            )
        )
    return out


def cache_analysis(results: list[RunResult]) -> list[dict]:
    """For each (endpoint,model) cache case: TTFT across attempts 0..N and cached_tokens.

    Returns rows: {endpoint, model, ttft_first, ttft_best_hit, ttft_drop_pct,
                   cached_tokens_max, caching_detected}
    """
    groups: dict[tuple, list[RunResult]] = {}
    for r in results:
        if r.case != "cache" or r.rep < 0:
            continue
        groups.setdefault((r.endpoint, r.model), []).append(r)

    def _eff_ttft(r: RunResult) -> float | None:
        # first-response TTFT: reasoning first-token for reasoning models, else content
        return r.reasoning_ttft_s if r.reasoning_ttft_s is not None else (r.ttft_content_s or r.ttft_s)

    rows: list[dict] = []
    for (endpoint, model), rs in sorted(groups.items()):
        successful = [r for r in rs if r.success and _eff_ttft(r) is not None]
        clean = [r for r in successful if not r.retried] or successful
        rs = sorted(clean, key=lambda r: r.rep)
        if not rs:
            continue
        first = _eff_ttft(rs[0])
        rest = [_eff_ttft(r) for r in rs[1:]] or [first]
        best_hit = min(rest)
        cached_max = max((r.usage.cached_tokens or 0) for r in rs)
        drop = (first - best_hit) / first * 100 if first else 0.0
        rows.append(
            {
                "endpoint": endpoint,
                "model": model,
                "ttft_first_ms": round(first * 1000, 1),
                "ttft_best_hit_ms": round(best_hit * 1000, 1),
                "ttft_drop_pct": round(drop, 1),
                "cached_tokens_max": int(cached_max),
                "caching_detected": bool(cached_max > 0 or drop > 15),
            }
        )
    return rows


def effort_analysis(results: list[RunResult]) -> list[dict]:
    """Reasoning-effort sweep: for each (endpoint,model), metrics per effort tier.

    Effort runs are tagged case="<base>@<tier>" (tier in default/low/medium/high).
    Returns rows: {endpoint, model, effort, reasoning_tok, content_tok,
                   ttft_content_ms, tps, e2e_s}
    """
    groups: dict[tuple, list[RunResult]] = {}
    for r in results:
        if r.rep < 0 or not r.success or "@" not in r.case:
            continue
        eff = r.case.split("@", 1)[1]
        groups.setdefault((r.endpoint, r.model, eff), []).append(r)

    rows: list[dict] = []
    for (endpoint, model, eff), rs in sorted(groups.items()):
        metric_rs = [r for r in rs if not r.retried] or rs
        reas = [(r.usage.output_tokens or 0) - (r.usage.content_tokens or 0) for r in metric_rs]
        cont = [r.usage.content_tokens for r in metric_rs if r.usage.content_tokens is not None]
        ttft = [(r.ttft_content_s or r.ttft_s) * 1000 for r in metric_rs if (r.ttft_content_s or r.ttft_s)]
        tps = [r.output_tps for r in metric_rs if r.output_tps]
        e2e = [r.e2e_s for r in metric_rs if r.e2e_s]
        rows.append(
            {
                "endpoint": endpoint,
                "model": model,
                "effort": eff,
                "reasoning_tok": round(_p50([float(x) for x in reas]) or 0),
                "content_tok": round(_p50(cont) or 0),
                "ttft_content_ms": round((_p50(ttft) or 0), 0),
                "tps": round((_p50(tps) or 0), 1),
                "e2e_s": round((_p50(e2e) or 0), 1),
            }
        )
    return rows
