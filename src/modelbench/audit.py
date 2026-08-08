"""Data-quality audit: is the benchmark data accurate and scientifically sound?

Run after a benchmark completes. Flags problems that would make a provider/model's
measured performance unrepresentative, each with a concrete re-test recommendation.

Audit dimensions:
  A. Coverage      — every endpoint/model has all expected cases, enough repeats.
  B. Truncation    — output hit max_output_tokens (incomplete) -> TPS/length biased.
  C. Estimation    — usage fell back to tiktoken (source=est) -> token counts suspect.
  D. Reliability   — low success rate / error clusters skew the latency sample.
  E. Variance      — huge TTFT/TPS spread -> single number not meaningful (needs more reps).
  F. Outliers      — warmup/cold-start or rate-limit spikes contaminating p50/p95.
  G. Reasoning mix — reasoning models whose thinking dominates -> content TPS tiny/Noisy.
  H. Cache validity — cache case actually exercised caching (cached_tokens>0 on later reps).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path

EXPECTED_CASES = ["S", "M", "L", "XL", "agent", "cache"]


@dataclass
class Issue:
    severity: str  # "high" | "med" | "low"
    target: str  # endpoint/model
    dimension: str
    detail: str
    recommendation: str


@dataclass
class AuditReport:
    n_rows: int
    n_models: int
    issues: list[Issue] = field(default_factory=list)
    model_summary: dict[str, dict] = field(default_factory=dict)

    def by_severity(self, sev: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == sev]


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def audit(results_path: Path, min_reps: int = 8) -> AuditReport:
    rows = _load(results_path)
    rows = [r for r in rows if r.get("rep", 0) >= 0]  # drop warmup
    models: dict[tuple, list[dict]] = {}
    for r in rows:
        models.setdefault((r["endpoint"], r["model"]), []).append(r)

    rep = AuditReport(n_rows=len(rows), n_models=len(models))

    for (endpoint, model), rs in sorted(models.items()):
        target = f"{endpoint}/{model}"
        by_case: dict[str, list[dict]] = {}
        for r in rs:
            by_case.setdefault(r["case"], []).append(r)

        # ---- A. coverage ----
        missing = [c for c in EXPECTED_CASES if c not in by_case]
        if missing:
            rep.issues.append(Issue("high", target, "coverage",
                                    f"缺 case: {missing}",
                                    "确认是否 probe 失败 / 该模型不支持 tool 或长输出；必要时单独补测"))
        for c, crs in by_case.items():
            if len(crs) < min_reps:
                rep.issues.append(Issue("med", target, "coverage",
                                        f"case {c} 仅 {len(crs)} 次重复 (<{min_reps})",
                                        "重复数不足，p95 不稳；加大 repeats 重测该 case"))

        # ---- per-case quality ----
        for c, crs in by_case.items():
            ok = [r for r in crs if r.get("success")]
            n = len(crs)
            succ = len(ok) / n if n else 0

            # D. reliability
            if succ < 0.8:
                errs = {}
                for r in crs:
                    if not r.get("success"):
                        errs[r.get("error_type")] = errs.get(r.get("error_type"), 0) + 1
                rep.issues.append(Issue("high", target, "reliability",
                                        f"case {c} 成功率 {succ:.0%}，错误分布 {errs}",
                                        "延迟样本被失败污染；查错误类型，限流则降并发重测"))

            # B. truncation
            trunc = [r for r in ok if r.get("finish_reason") and "max_output" in str(r["finish_reason"])]
            if ok and len(trunc) / len(ok) > 0.3:
                rep.issues.append(Issue("med", target, "truncation",
                                        f"case {c} {len(trunc)}/{len(ok)} 触发 max_output 截断",
                                        "输出被砍断，TPS/长度偏低失真；提高 max_output_tokens 重测"))

            # C. estimation fallback
            est = [r for r in ok if r.get("usage_source") == "est"]
            if ok and len(est) / len(ok) > 0.5:
                rep.issues.append(Issue("med", target, "estimation",
                                        f"case {c} {len(est)}/{len(ok)} token 用 tiktoken 估算",
                                        "该 endpoint 不回传 usage；TPS 基于估算，标注并谨慎解读"))

            # E. variance (TTFT) — only meaningful with enough samples
            ttfts = [ (r.get("ttft_content_s") or r.get("ttft_s")) for r in ok]
            ttfts = [t for t in ttfts if t]
            if len(ttfts) >= 5:
                med = statistics.median(ttfts)
                sd = statistics.pstdev(ttfts)
                if med > 0 and sd / med > 0.6:
                    rep.issues.append(Issue("med", target, "variance",
                                            f"case {c} TTFT 变异系数 {sd/med:.2f} (中位{med*1000:.0f}ms σ{sd*1000:.0f}ms)",
                                            "TTFT 抖动大，可能是冷启动/排队；增加 repeats 或排除首请求重测"))

            # G. reasoning dominance
            if c in ("S", "M", "L", "XL"):
                outs = [r for r in ok if r.get("output_tokens") and r.get("content_tokens") is not None]
                if outs:
                    frac = [r["content_tokens"]/r["output_tokens"] for r in outs if r["output_tokens"]]
                    if frac and statistics.median(frac) < 0.25:
                        rep.issues.append(Issue("low", target, "reasoning_mix",
                                                f"case {c} 正文仅占总输出 {statistics.median(frac):.0%}(思考过重)",
                                                "reasoning 模型思考主导；解读 TPS 时用 content TPS 而非总 TPS"))

        # ---- H. cache validity ----
        if "cache" in by_case:
            cache_ok = sorted([r for r in by_case["cache"] if r.get("success")], key=lambda r: r["rep"])
            cached = [r.get("cached_tokens") or 0 for r in cache_ok]
            if len(cache_ok) >= 3 and max(cached) == 0:
                rep.issues.append(Issue("low", target, "cache",
                                        "cache case 未检测到 cached_tokens",
                                        "该 provider 可能不支持 prefix 缓存或前缀未达阈值；解读缓存维度时排除"))

        # per-model rollup
        all_ttft = [ (r.get("ttft_content_s") or r.get("ttft_s")) for r in rs if r.get("success")]
        all_ttft = [t for t in all_ttft if t]
        all_tps = [r.get("output_tps") for r in rs if r.get("success") and r.get("output_tps")]
        rep.model_summary[target] = {
            "rows": len(rs),
            "cases": sorted(by_case.keys()),
            "success_rate": round(sum(1 for r in rs if r.get("success")) / len(rs), 3),
            "ttft_p50_ms": round(statistics.median(all_ttft) * 1000, 0) if all_ttft else None,
            "tps_p50": round(statistics.median(all_tps), 1) if all_tps else None,
        }

    # sort issues: high first
    order = {"high": 0, "med": 1, "low": 2}
    rep.issues.sort(key=lambda i: (order[i.severity], i.target))
    return rep


def render_audit(rep: AuditReport) -> str:
    lines = ["# 数据质量审计\n"]
    lines.append(f"- 样本 {rep.n_rows} 行，{rep.n_models} 个端点/模型")
    hi = rep.by_severity("high")
    med = rep.by_severity("med")
    low = rep.by_severity("low")
    lines.append(f"- 问题：🔴高 {len(hi)} · 🟡中 {len(med)} · ⚪低 {len(low)}\n")

    lines.append("\n## 模型总览\n")
    lines.append("| 端点/模型 | 行数 | 成功率 | TTFT p50(ms) | TPS p50 | case 覆盖 |")
    lines.append("|---|---|---|---|---|---|")
    for t, s in sorted(rep.model_summary.items()):
        lines.append(
            f"| {t} | {s['rows']} | {s['success_rate']*100:.0f}% | "
            f"{s['ttft_p50_ms'] or '-'} | {s['tps_p50'] or '-'} | {len(s['cases'])}/6 |"
        )

    for sev, icon in [("high", "🔴"), ("med", "🟡"), ("low", "⚪")]:
        iss = rep.by_severity(sev)
        if not iss:
            continue
        lines.append(f"\n## {icon} {sev.upper()} 严重度问题\n")
        lines.append("| 端点/模型 | 维度 | 问题 | 建议 |")
        lines.append("|---|---|---|---|")
        for i in iss:
            lines.append(f"| {i.target} | {i.dimension} | {i.detail} | {i.recommendation} |")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    p = Path(sys.argv[1])
    r = audit(p)
    print(render_audit(r))
