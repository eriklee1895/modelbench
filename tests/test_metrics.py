"""Unit tests for metric derivation and aggregation (no network)."""

from modelbench.metrics import aggregate, cache_analysis
from modelbench.models import RunResult, TokenUsage


def _mk(case="S", rep=0, **kw) -> RunResult:
    r = RunResult(endpoint="ep", group="volc", vendor="v", model="m", case=case, rep=rep)
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def test_plain_model_tps():
    r = _mk(success=True, ttft_s=0.5, e2e_s=2.5)
    r.usage = TokenUsage(input_tokens=100, output_tokens=400, content_tokens=400, source="api")
    r.derive()
    # decode window = 2.5 - 0.5 = 2.0s; 400/2.0 = 200 tps
    assert r.output_tps == 200.0
    assert r.e2e_tps == 160.0


def test_reasoning_model_uses_content_window():
    # reasoning: thinking 0.5->5.0s, content 5.0->7.0s (2s window), 300 content tokens
    r = _mk(success=True, ttft_s=None, reasoning_ttft_s=0.5, ttft_content_s=5.0, e2e_s=7.0)
    r.usage = TokenUsage(input_tokens=100, output_tokens=1300, content_tokens=300, source="api")
    r.derive()
    assert r.output_tps == 150.0  # 300 / (7-5), NOT 1300/(7-0.5)


def test_content_fallback_to_output():
    r = _mk(success=True, ttft_s=1.0, e2e_s=3.0)
    r.usage = TokenUsage(input_tokens=10, output_tokens=100, content_tokens=None, source="est")
    r.derive()
    assert r.output_tps == 50.0


def test_aggregate_success_rate():
    rs = [
        _mk(success=True, ttft_s=1.0, e2e_s=2.0),
        _mk(success=True, ttft_s=1.2, e2e_s=2.2),
        _mk(success=False, error_type="timeout"),
    ]
    for r in rs:
        r.usage = TokenUsage(output_tokens=10, content_tokens=10)
        r.derive()
    stats = aggregate(rs)
    assert len(stats) == 1
    s = stats[0]
    assert s.n == 3 and s.n_success == 2
    assert abs(s.success_rate - 2 / 3) < 1e-6
    assert s.ttft_p50 is not None


def test_cache_analysis_detects_hit():
    rs = [
        _mk(case="cache", rep=0, success=True, ttft_s=1.0),
        _mk(case="cache", rep=1, success=True, ttft_s=0.3),
        _mk(case="cache", rep=2, success=True, ttft_s=0.25),
    ]
    rs[0].usage = TokenUsage(cached_tokens=0)
    rs[1].usage = TokenUsage(cached_tokens=5800)
    rs[2].usage = TokenUsage(cached_tokens=5800)
    rows = cache_analysis(rs)
    assert len(rows) == 1
    row = rows[0]
    assert row["cached_tokens_max"] == 5800
    assert row["caching_detected"] is True
    assert row["ttft_drop_pct"] > 50
