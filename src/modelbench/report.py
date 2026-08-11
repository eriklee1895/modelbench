"""Report: aggregate stats -> Markdown tables + PNG charts."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from .engine import read_results
from .metrics import Stats, aggregate, cache_analysis, effort_analysis

# CJK-capable fonts so Chinese chart titles/labels render correctly
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS", "STHeiti"]
plt.rcParams["axes.unicode_minus"] = False


def _f(x: float | None, nd: int = 1, scale: float = 1.0) -> str:
    if x is None:
        return "-"
    return f"{x * scale:.{nd}f}"


def _label(s: Stats) -> str:
    return f"{s.endpoint}/{s.model}"


def _bar(ax, labels, values, title, ylabel, color):
    vals = [v if v is not None else 0 for v in values]
    ax.bar(range(len(labels)), vals, color=color)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=7)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.3)


def make_charts(stats: list[Stats], out_dir: Path, stem: str) -> list[Path]:
    paths: list[Path] = []
    core_cases = ["S", "M", "L", "XL"]

    # 1. TPS by model (L case) — the headline decode-rate comparison
    def _dtps(s: Stats) -> float:
        return (s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50) or 0

    l_stats = [s for s in stats if s.case == "L" and _dtps(s)]
    if l_stats:
        l_stats.sort(key=_dtps, reverse=True)
        fig, ax = plt.subplots(figsize=(11, 4.5))
        colors = ["#e0702a" if s.group == "volc" else "#2a6de0" for s in l_stats]
        _bar(
            ax,
            [_label(s) for s in l_stats],
            [_dtps(s) for s in l_stats],
            "解码 TPS (chunk间客户端观测速率) — case L, p50",
            "tokens/s",
            "#2a6de0",
        )
        for patch, c in zip(ax.patches, colors, strict=False):
            patch.set_color(c)
        fig.tight_layout()
        p = out_dir / f"{stem}_tps_L.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    # 2. TTFT by model (L case) — effective first-response TTFT
    if l_stats:
        fig, ax = plt.subplots(figsize=(11, 4.5))
        eff = [
            ((s.reasoning_ttft_p50 if s.is_reasoning else (s.ttft_content_p50 or s.ttft_p50)) or 0) * 1000
            for s in l_stats
        ]
        _bar(ax, [_label(s) for s in l_stats], eff, "首响应 TTFT — case L, p50", "ms", "#3aa17e")
        fig.tight_layout()
        p = out_dir / f"{stem}_ttft_L.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    # 3. Official vs Volc, same model (paired TPS + TTFT)
    by_model: dict[str, dict[str, Stats]] = {}
    for s in stats:
        if s.case != "L":
            continue
        by_model.setdefault(s.model, {})[s.group] = s
    paired = {m: g for m, g in by_model.items() if "official" in g and "volc" in g}
    if paired:
        models = sorted(paired)

        def _eff(s: Stats) -> float:
            v = s.reasoning_ttft_p50 if s.is_reasoning else (s.ttft_content_p50 or s.ttft_p50)
            return (v or 0) * 1000

        def _t(s: Stats) -> float:
            return (s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50) or 0

        off_tps = [_t(paired[m]["official"]) for m in models]
        volc_tps = [_t(paired[m]["volc"]) for m in models]
        off_ttft = [_eff(paired[m]["official"]) for m in models]
        volc_ttft = [_eff(paired[m]["volc"]) for m in models]
        x = range(len(models))
        fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
        w = 0.4
        axes[0].bar([i - w / 2 for i in x], off_tps, width=w, label="official", color="#2a6de0")
        axes[0].bar([i + w / 2 for i in x], volc_tps, width=w, label="volc-plan", color="#e0702a")
        axes[0].set_title("TPS — official vs Volc (case L)")
        axes[0].set_ylabel("tokens/s")
        axes[1].bar([i - w / 2 for i in x], off_ttft, width=w, label="official", color="#2a6de0")
        axes[1].bar([i + w / 2 for i in x], volc_ttft, width=w, label="volc-plan", color="#e0702a")
        axes[1].set_title("TTFT — official vs Volc (case L)")
        axes[1].set_ylabel("ms")
        for ax in axes:
            ax.set_xticks(list(x))
            ax.set_xticklabels(models, rotation=40, ha="right", fontsize=8)
            ax.legend()
            ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        p = out_dir / f"{stem}_official_vs_volc.png"
        fig.savefig(p, dpi=130)
        plt.close(fig)
        paths.append(p)

    # 4. TPS across cases for top models (scaling behaviour)
    def _dt(s: Stats) -> float | None:
        return s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50

    fig, ax = plt.subplots(figsize=(11, 4.5))
    models_seen = []
    for s in sorted(stats, key=lambda s: _dt(s) or 0, reverse=True):
        if s.case == "L" and _label(s) not in models_seen:
            models_seen.append(_label(s))
    top = models_seen[:8]
    for lbl in top:
        series = [next((_dt(s) for s in stats if _label(s) == lbl and s.case == c), None) for c in core_cases]
        ax.plot(core_cases, [v or 0 for v in series], marker="o", label=lbl, linewidth=1.4)
    ax.set_title("解码 TPS scaling across input sizes (p50)")
    ax.set_ylabel("tokens/s")
    ax.set_xlabel("case (input size)")
    ax.legend(fontsize=6, ncol=2)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    p = out_dir / f"{stem}_tps_scaling.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    paths.append(p)

    return paths


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r) + " |")
    return "\n".join(out)


def make_effort_charts(effort_rows: list[dict], out_dir: Path, stem: str) -> list[Path]:
    """Grouped bars: per model, first-content TTFT and reasoning tokens by effort tier."""
    models = sorted({(r["endpoint"], r["model"]) for r in effort_rows})
    tiers = ["default", "low", "medium", "high"]
    labels = [f"{e.split('-')[0]}/{m}" for e, m in models]

    def val(ep_m, tier, key):
        row = next((r for r in effort_rows if (r["endpoint"], r["model"]) == ep_m and r["effort"] == tier), None)
        return row[key] if row else 0

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    w = 0.2
    x = range(len(models))
    colors = {"default": "#888888", "low": "#3aa17e", "medium": "#e0a52a", "high": "#c0392b"}
    for i, tier in enumerate(tiers):
        axes[0].bar([j + i * w for j in x], [val(m, tier, "ttft_content_ms") for m in models], width=w, label=tier, color=colors[tier])
        axes[1].bar([j + i * w for j in x], [val(m, tier, "reasoning_tok") for m in models], width=w, label=tier, color=colors[tier])
    axes[0].set_title("首正文 TTFT by reasoning 档位 (case M, p50)")
    axes[0].set_ylabel("ms")
    axes[1].set_title("思考 token 量 by reasoning 档位 (case M, p50)")
    axes[1].set_ylabel("tokens")
    for ax in axes:
        ax.set_xticks([j + 1.5 * w for j in x])
        ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=6)
        ax.legend(fontsize=7)
        ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    p = out_dir / f"{stem}_effort.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return [p]


def make_scorecard_chart(scored: list[dict], out_dir: Path, stem: str) -> list[Path]:
    """Grouped horizontal bars: throughput-first vs responsiveness-first scores."""
    s = sorted(scored, key=lambda r: r["tp"])
    labels = [r["label"] for r in s]
    tp = [r["tp"] for r in s]
    resp = [r["resp"] for r in s]
    y = list(range(len(labels)))
    h = 0.4
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.barh([i + h / 2 for i in y], tp, height=h, label="吞吐优先", color="#2a6de0")
    ax.barh([i - h / 2 for i in y], resp, height=h, label="响应优先", color="#3aa17e")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("得分 (0-100, 越高越好)")
    ax.set_title("综合对比：吞吐优先 vs 响应优先 (case L)")
    ax.legend()
    ax.grid(axis="x", alpha=0.3)
    fig.tight_layout()
    p = out_dir / f"{stem}_scorecard.png"
    fig.savefig(p, dpi=130)
    plt.close(fig)
    return [p]


def write_report(results_path: Path, out_dir: Path, effort_path: Path | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    results = read_results(results_path)
    stats = aggregate(results)
    cache_rows = cache_analysis(results)
    stem = results_path.stem
    # exclude effort-tier rows (case contains '@') from the main per-case tables
    stats = [s for s in stats if "@" not in s.case]
    effort_rows: list[dict] = []
    if effort_path and effort_path.exists():
        effort_rows = effort_analysis(read_results(effort_path))
    charts = make_charts(stats, out_dir, stem)
    if effort_rows:
        charts += make_effort_charts(effort_rows, out_dir, stem)

    lines: list[str] = []
    lines.append("# 模型性能 Benchmark 报告\n")
    lines.append(f"- 数据源: `{results_path.name}`")
    lines.append(
        f"- 总样本: {len(results)} 次请求，覆盖 {len({(s.endpoint, s.model) for s in stats})} 个端点/模型组合"
    )
    lines.append("- 协议: 全部走 OpenAI **Responses API**（stream=true）")
    lines.append(
        "- 指标: **首响应TTFT**=首个任意输出(reasoning 模型=首思考, 普通=首正文)；**首正文TTFT**=首个可见答案 token(reasoning 模型需先想完)；"
        "**解码TPS**=首个正文chunk之后的估算token/(末chunk−首chunk) 客户端观测速率；**端到端TPS**=正文token/总时长(最接近体感)；E2E=请求→completed\n"
    )

    # ---- headline: case L comparison ----
    lines.append("\n## 一、核心对比（case L：in~4k / out~1k，p50）\n")

    def _tps(s: Stats) -> float | None:
        # client-observed chunk rate; fall back to usage-derived throughput
        return s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50

    core = sorted([s for s in stats if s.case == "L"], key=lambda s: _tps(s) or 0, reverse=True)
    rows = []
    for s in core:
        first_resp = s.reasoning_ttft_p50 if s.is_reasoning else (s.ttft_content_p50 or s.ttft_p50)
        name = ("🧠" if s.is_reasoning else "") + _label(s)
        rows.append(
            [
                name,
                "🟠Volc" if s.group == "volc" else "🔵官方",
                _f(first_resp, 0, 1000),
                _f(s.ttft_content_p50 or s.ttft_p50, 0, 1000),
                f"**{_f(_tps(s))}**",
                _f(s.e2e_tps_p50),
                _f(s.e2e_p50, 1, 1),
                f"{s.success_rate * 100:.0f}%",
            ]
        )
    lines.append(
        _md_table(
            [
                "端点/模型",
                "来源",
                "首响应TTFT(ms)",
                "首正文TTFT(ms)",
                "解码TPS(tok/s)",
                "端到端TPS(体感)",
                "E2E(s)",
                "成功率",
            ],
            rows,
        )
    )
    lines.append(
        "\n> 🧠=reasoning 模型。**解码TPS**=首个正文chunk之后的客户端观测速率（chunk计时）；**端到端TPS**=含首延迟摊薄，最接近日常体感。"
        "具体结论以本轮表格和数据质量审计为准。"
    )

    # ---- 综合评分报表 (scorecard) ----
    # 归一化内容TPS 与首正文TTFT，加权成 0-100 综合速度得分（越高越快）
    def _body_ttft(s: Stats) -> float | None:
        v = s.ttft_content_p50 or s.ttft_p50
        return v * 1000 if v is not None else None

    tps_vals = [_tps(s) for s in core if _tps(s)]
    ttft_vals = [_body_ttft(s) for s in core if _body_ttft(s)]
    max_tps = max(tps_vals) if tps_vals else 1
    min_ttft = min(ttft_vals) if ttft_vals else 1
    scored: list[dict] = []
    for s in core:
        tps_n = (_tps(s) or 0) / max_tps
        bt = _body_ttft(s)
        ttft_n = (min_ttft / bt) if bt else 0  # lower TTFT -> higher score
        ttft_n = (min_ttft / bt) if bt else 0  # lower TTFT -> higher score
        # throughput-first: long-form / batch generation
        tp_score = 100 * (0.8 * tps_n + 0.2 * ttft_n)
        # responsiveness-first: interactive agent (TTFT matters most)
        resp_score = 100 * (0.8 * ttft_n + 0.2 * tps_n)
        scored.append(
            {
                "label": _label(s),
                "group": s.group,
                "tp": round(tp_score, 1),
                "resp": round(resp_score, 1),
                "s": s,
            }
        )
    # rank by throughput-first for the main table
    scored.sort(key=lambda r: r["tp"], reverse=True)

    lines.append("\n\n## 二、综合对比报表（case L）\n")
    lines.append(
        "> 双维度评分（0-100，越高越好）：**吞吐优先**=80%解码TPS+20%首正文TTFT（长文/批量生成）；**响应优先**=80%首正文TTFT+20%解码TPS（交互式 Agent，首延迟为王）。"
        "单一分数会误导，具体排名以本轮表格和数据质量审计为准。\n"
    )
    rows = []
    for i, r in enumerate(scored, 1):
        s = r["s"]
        rows.append(
            [
                str(i),
                ("🧠" if s.is_reasoning else "") + _label(s),
                "🟠" if s.group == "volc" else "🔵",
                f"**{r['tp']}**",
                f"**{r['resp']}**",
                _f(s.ttft_content_p50 or s.ttft_p50, 0, 1000),
                _f(_tps(s)),
                _f(s.e2e_tps_p50),
                _f(s.e2e_p50, 1, 1),
                f"{s.success_rate * 100:.0f}%",
            ]
        )
    lines.append(
        _md_table(
            ["#", "端点/模型", "源", "吞吐优先", "响应优先", "首正文TTFT(ms)", "解码TPS", "端到端TPS", "E2E(s)", "成功率"],
            rows,
        )
    )
    scorecard_chart = make_scorecard_chart(scored, out_dir, stem)
    lines.append(f"\n![综合得分]({scorecard_chart[0].name})")

    # ---- per-case detail ----
    lines.append("\n\n## 三、分负载明细（p50 / p95）\n")
    for case in ["S", "M", "L", "XL", "agent"]:
        cs = sorted(
            [s for s in stats if s.case == case],
            key=lambda s: (s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50) or 0,
            reverse=True,
        )
        if not cs:
            continue
        lines.append(f"\n### case {case}\n")
        rows = []
        for s in cs:
            # effective first-response TTFT (reasoning->first think, else first content)
            eff50 = s.reasoning_ttft_p50 if s.is_reasoning else (s.ttft_content_p50 or s.ttft_p50)
            body = s.ttft_content_p50 or s.ttft_p50
            d50 = s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50
            d95 = s.decode_tps_p95 if s.decode_tps_p95 is not None else s.tps_p95
            name = ("🧠" if s.is_reasoning else "") + _label(s)
            rows.append(
                [
                    name,
                    _f(eff50, 0, 1000),
                    _f(body, 0, 1000),
                    _f(d50) + " / " + _f(d95),
                    _f(s.tps_std, 2),
                    _f(s.e2e_p50, 2) + " / " + _f(s.e2e_p95, 2),
                    _f(s.out_tokens_p50, 0),
                    f"{s.success_rate * 100:.0f}% ({s.n_success}/{s.n})",
                ]
            )
        lines.append(
            _md_table(
                [
                    "端点/模型",
                    "首响应ms",
                    "首正文ms",
                    "解码TPS (p50/p95)",
                    "TPS σ",
                    "E2E s (p50/p95)",
                    "输出tok",
                    "成功率",
                ],
                rows,
            )
        )

    # ---- official vs volc ----
    lines.append("\n\n## 四、官方直连 vs Volc Agent Plan（同模型）\n")
    by_model: dict[str, dict[str, Stats]] = {}
    for s in stats:
        if s.case == "L":
            by_model.setdefault(s.model, {})[s.group] = s
    rows = []

    def _effms(s: Stats) -> float:
        v = s.reasoning_ttft_p50 if s.is_reasoning else (s.ttft_content_p50 or s.ttft_p50)
        return (v or 0) * 1000

    def _dt2(s: Stats) -> float:
        return (s.decode_tps_p50 if s.decode_tps_p50 is not None else s.tps_p50) or 0

    for m, g in sorted(by_model.items()):
        if "official" in g and "volc" in g:
            o, v = g["official"], g["volc"]
            tps_delta = _dt2(v) - _dt2(o)
            ttft_delta = _effms(v) - _effms(o)
            faster = "Volc" if tps_delta > 0 else "官方"
            rows.append(
                [
                    m,
                    _f(_effms(o), 0),
                    _f(_effms(v), 0),
                    _f(ttft_delta, 0),
                    _f(_dt2(o)),
                    _f(_dt2(v)),
                    _f(tps_delta),
                    faster,
                ]
            )
    if rows:
        lines.append(
            _md_table(
                [
                    "模型",
                    "官方TTFT(ms)",
                    "Volc TTFT(ms)",
                    "ΔTTFT(ms)",
                    "官方TPS",
                    "Volc TPS",
                    "ΔTPS",
                    "TPS更快",
                ],
                rows,
            )
        )
        lines.append("\n> Δ 为正 = Volc 更慢/更高；ΔTPS 为正 = Volc 更快。")
    else:
        lines.append("\n（无两侧都成功的重叠模型）")

    # ---- agent ----
    lines.append("\n\n## 五、Agent 专项（tool_call 决策）\n")
    ag = sorted([s for s in stats if s.case == "agent"], key=lambda s: s.tool_ttft_p50 or 1e9)
    rows = []
    for s in ag:
        rows.append(
            [
                _label(s),
                _f(s.tool_ttft_p50, 0, 1000),
                _f(s.ttft_p50, 0, 1000),
                _f(s.e2e_p50, 2),
                f"{(s.tool_valid_rate or 0) * 100:.0f}%",
                f"{s.success_rate * 100:.0f}%",
            ]
        )
    lines.append(
        _md_table(["端点/模型", "tool_call首延迟(ms)", "TTFT(ms)", "E2E(s)", "tool合法性", "成功率"], rows)
    )

    # ---- cache ----
    lines.append("\n\n## 六、Prompt Caching 效果\n")
    if cache_rows:
        rows = []
        for cr in sorted(cache_rows, key=lambda r: r["ttft_drop_pct"], reverse=True):
            rows.append(
                [
                    f"{cr['endpoint']}/{cr['model']}",
                    _f(cr["ttft_first_ms"], 0),
                    _f(cr["ttft_best_hit_ms"], 0),
                    f"{cr['ttft_drop_pct']}%",
                    str(cr["cached_tokens_max"]),
                    "✅" if cr["caching_detected"] else "—",
                ]
            )
        lines.append(
            _md_table(
                [
                    "端点/模型",
                    "首次TTFT(ms)",
                    "命中最优TTFT(ms)",
                    "TTFT降幅",
                    "max cached_tokens",
                    "检出缓存",
                ],
                rows,
            )
        )
    else:
        lines.append("\n（无 cache 数据）")

    # ---- charts ----
    lines.append("\n\n## 七、图表\n")
    for p in charts:
        lines.append(f"![{p.stem}]({p.name})")

    # ---- reasoning effort sweep ----
    if effort_rows:
        lines.append("\n\n## 八、Reasoning 档位影响（case M，p50）\n")
        lines.append(
            "> 同一模型在 default/low/medium/high 四档下的思考量、首正文延迟、内容TPS、E2E。**思考量与首正文TTFT随档位上升，内容TPS基本不变**——调高档位的代价在延迟，不在解码速度。\n"
        )
        models_in_sweep = sorted({(r["endpoint"], r["model"]) for r in effort_rows})
        rows = []
        for ep_m in models_in_sweep:
            for tier in ["default", "low", "medium", "high"]:
                r = next((x for x in effort_rows if (x["endpoint"], x["model"]) == ep_m and x["effort"] == tier), None)
                if not r:
                    continue
                lbl = f"{r['endpoint'].replace('-official', '').replace('-plan', '')}/{r['model']}"
                rows.append(
                    [
                        lbl,
                        tier,
                        str(r["reasoning_tok"]),
                        str(r["content_tok"]),
                        f"{r['ttft_content_ms']:.0f}",
                        _f(r["tps"]),
                        _f(r["e2e_s"], 1),
                    ]
                )
        lines.append(
            _md_table(
                ["端点/模型", "档位", "思考tok", "正文tok", "首正文TTFT(ms)", "内容TPS", "E2E(s)"],
                rows,
            )
        )

    # ---- takeaway ----
    lines.append("\n\n## 九、结论与 Agent 任务选型建议\n")

    # --- decode speed ranking (authoritative decode TPS, case L) ---
    if core:
        lines.append("\n### 解码速度（chunk计时纯解码 TPS，case L）\n")
        lines.append("\n| 排名 | 端点/模型 | 解码TPS | 端到端TPS | 适合 |")
        lines.append("|---|---|---|---|---|")
        for i, s in enumerate(core[:5], 1):
            fit = {1: "长文生成/高吞吐 Agent 首选", 2: "高吞吐备选"}.get(i, "")
            lines.append(f"| {i} | {_label(s)} | **{_f(_tps(s))}** | {_f(s.e2e_tps_p50)} | {fit} |")

    # --- Agent step latency (tool_call first-token) ---
    if ag:
        lines.append("\n### Agent 步进延迟（tool_call 首延迟，越短 Agent 越跟手）\n")
        best = [s for s in ag if (s.tool_valid_rate or 0) >= 0.95 and s.tool_ttft_p50]
        best.sort(key=lambda s: s.tool_ttft_p50)
        if best:
            lines.append("\n| 端点/模型 | tool首延迟(ms) | 单步E2E(s) |")
            lines.append("|---|---|---|")
            for s in best[:5]:
                lines.append(f"| {_label(s)} | {_f(s.tool_ttft_p50, 0, 1000)} | {_f(s.e2e_p50, 2)} |")

    lines.append("\n### 关键结论（本轮实测，TPS 为客户端观测的 chunk 间速率）\n")
    if core:
        top_tps = core[0]
        fastest_response = min(core, key=lambda s: _body_ttft(s) or float("inf"))
        lines.append(
            f"0. **case L 解码速率第一**：{_label(top_tps)}，p50 {_f(_tps(top_tps))} tok/s；"
            f"**首正文延迟最低**：{_label(fastest_response)}，p50 {_f(_body_ttft(fastest_response), 0)} ms。"
            "两者是不同优化目标，不能用一个数字替代。"
        )
    if by_model:
        lines.append("1. **官方直连与 Volc Plan 的差异应按同模型配对表解读**，托管路径、网关排队和配额都会改变结果。")
    reasoning_core = [s for s in core if s.is_reasoning]
    if reasoning_core:
        slowest_answer = max(reasoning_core, key=lambda s: _body_ttft(s) or 0)
        lines.append(
            f"2. **Reasoning 模型必须同时看两档 TTFT**：本轮 case L 中，{_label(slowest_answer)} 的首正文 p50 为 "
            f"{_f(_body_ttft(slowest_answer), 0)} ms；首响应和首正文不是同一用户体验指标。"
        )
    if cache_rows:
        best_cache = max(cache_rows, key=lambda r: r["ttft_drop_pct"])
        lines.append(
            f"3. **缓存效果以 cached_tokens 和 TTFT 降幅共同确认**：本轮最大降幅为 "
            f"{best_cache['endpoint']}/{best_cache['model']} 的 {best_cache['ttft_drop_pct']}%。"
        )
    failures = [r for r in results if r.rep >= 0 and not r.success]
    if failures:
        lines.append(f"4. **本轮有 {len(failures)} 个失败样本**，成功率应按 case 查看；失败不会被隐藏在延迟 p50 中。")
    if effort_rows:
        lines.append("5. **Reasoning effort 档位**请结合思考 token、首正文 TTFT 和内容 TPS 一起判断，不能只看档位名称。")

    lines.append("\n### 按场景推荐\n")
    recommendation_rows: list[list[str]] = []
    if core:
        throughput_models = " / ".join(_label(s) for s in core[:2])
        throughput_values = " / ".join(_f(_tps(s)) for s in core[:2])
        fastest_response = min(core, key=lambda s: _body_ttft(s) or float("inf"))
        recommendation_rows.append(
            ["高吞吐长文生成", throughput_models, f"case L 解码 TPS p50 {throughput_values} tok/s"]
        )
        recommendation_rows.append(
            ["交互式 Agent", _label(fastest_response), f"case L 首正文 TTFT p50 {_f(_body_ttft(fastest_response), 0)} ms"]
        )
    if cache_rows:
        best_cache = max(cache_rows, key=lambda r: r["ttft_drop_pct"])
        recommendation_rows.append(
            [
                "固定长 prompt + 高频调用",
                f"{best_cache['endpoint']}/{best_cache['model']}",
                f"缓存检测={best_cache['caching_detected']}，TTFT 降幅 {best_cache['ttft_drop_pct']}%",
            ]
        )
    if ag:
        tool_candidates = [s for s in ag if (s.tool_valid_rate or 0) >= 0.95 and s.tool_ttft_p50]
        if tool_candidates:
            tool_best = min(tool_candidates, key=lambda s: s.tool_ttft_p50 or float("inf"))
            recommendation_rows.append(
                ["工具编排", _label(tool_best), f"tool 首延迟 p50 {_f(tool_best.tool_ttft_p50, 0, 1000)} ms"]
            )
    lines.append(_md_table(["场景", "推荐", "理由"], recommendation_rows))

    report_path = out_dir / f"report_{stem.removeprefix('raw_')}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
