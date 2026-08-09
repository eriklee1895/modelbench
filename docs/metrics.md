# Methodology

[English](metrics.md) | [中文](metrics_cn.md)

How modelbench measures LLM performance, and why. This is the "why" behind the numbers — for usage see [README](../README.md). For a worked example see [examples/](../examples/).

## The problem this solves

A naive TPS/TTFT benchmark breaks down on **reasoning models** (DeepSeek, Kimi, GLM, Doubao, …). These models emit a hidden *thinking* stream before the visible answer, which causes three measurement traps:

1. **Thinking tokens counted as output inflate TPS** — the model appears fast because you're counting tokens the user never sees.
2. **TTFT measured to the first *thinking* token hides the real time-to-answer** — "first token in 0.3s" sounds great, but the answer starts 40s later.
3. **Streaming without `usage` breaks token counts entirely** — e.g. DeepSeek's official Responses endpoint does not return `usage` on streamed responses, so any metric that depends on it silently fails.

modelbench is designed around these realities.

## Core measurements

### Decode TPS (the authoritative speed number)

```
decode_tps = content_tokens / (last_content_chunk_time − first_content_chunk_time)
```

- Tokens are **content (visible answer) tokens only** — thinking tokens are excluded.
- The decode window starts at the **first visible content chunk by construction**, so thinking time is never in the denominator.
- Timing comes from **per-chunk arrival timestamps**, not from `usage` — so it works even when the provider doesn't return usage in streaming mode.

This makes decode TPS the most stable, comparable number across providers. It answers "once the model starts answering, how fast does it emit text?"

### First-response TTFT vs first-content TTFT

We record **two** TTFTs separately:

| Metric | Stops at | Meaning |
|---|---|---|
| First-response TTFT | first output event (first *thinking* token for reasoning models) | model has started; queueing + prefill + first token |
| **First-content TTFT** | first **visible answer** token | when the user actually starts seeing the answer |

For a non-reasoning model these are identical. For a reasoning model they can differ by tens of seconds (thinking length), and the second is what users actually feel.

> A single "TTFT" number on a reasoning model is misleading — always look at first-content TTFT for agent UX, and first-response TTFT if you care about backend queueing.

### End-to-end TPS (perceived speed)

```
e2e_tps = content_tokens / total_request_time
```

Total time from request to `response.completed`. This is the closest single number to perceived speed because it includes first-token latency. A model can have a huge decode TPS but terrible e2e TPS if it thinks for 50s before answering (looking at you, GLM-5.2).

### E2E latency

Wall-clock request → `response.completed`. Useful for SLA-style comparisons.

### Prompt-caching effect

The `cache` workload sends the same ~6k-token prefix 10 times. We record:
- `cached_tokens` reported by the API (proves caching actually happened),
- first-response TTFT of the first (cold) vs best (warm) attempt,
- the % drop.

Every provider we tested supports prefix caching; the drop ranges from ~10% to ~80% depending on the model.

### Reasoning-effort tiers

The `effort` command runs each model at `low / medium / high` reasoning effort (plus provider default). The key empirical finding:

> **Thinking amount and first-content TTFT rise sharply with effort tier, but decode TPS barely changes.** The cost of "thinking harder" is all in latency, not in decode speed. And on many models `default ≈ high` — it's `low` that actually saves time.

A caveat: some providers (GLM, MiniMax on some plans) silently ignore the `reasoning.effort` parameter. The sweep makes that visible by comparing thinking-token counts across tiers.

### Tool-call latency & validity (agent workload)

The `agent` workload sends a tool-rich prompt and measures time to the first `function_call` item. Crucially, **validity** is whether the function call has parseable arguments — a 200 response with no call is a failure, not a success. This catches models that "support tools" but don't reliably emit them (we found DeepSeek's official endpoint rejects forced `tool_choice` in thinking mode, for example).

## How we keep numbers honest

### Data-quality auditor

`modelbench.audit` flags, per (model, case):

- **reliability** — success rate; anything below ~90% means the latency numbers are sampled from a biased subset.
- **empty responses** — `incomplete:length` means the model spent its entire output budget on thinking and produced no answer (common on aggressive reasoners). This is a real failure mode for production, not a measurement error.
- **truncation** — outputs hitting `max_output_tokens` mean the TPS window is abnormal.
- **token-estimation fallback** — when `usage` is missing, we count with tiktoken; the audit flags this so you know which numbers are estimates.
- **TTFT variance** — high coefficient of variation usually means thinking length is random (not measurement error); compare it against first-response TTFT's CV to tell queueing noise from thinking-length noise.

### Experimental controls

- **Serial within a (model, case)** — repeats run one after another so they don't compete for the same backend.
- **Concurrent across models** — different models hit different backends, so this is safe; a per-endpoint semaphore bounds concurrency to avoid tripping rate limits (which would otherwise look like inflated TTFT).
- **10 repeats per cell**, with 1 warmup — median is the reported statistic; p95 and std are also captured.
- **`temperature=0`** and fixed output budgets so output lengths are comparable across models.

## Known pitfalls (things we hit, so you don't have to)

- **DeepSeek official streaming omits `usage`.** Decode TPS falls back to tiktoken chunk counting; this is exactly why chunk timing exists.
- **`tool_choice: "required"` is rejected in DeepSeek's thinking mode.** We degrade to `auto` there; it still elicits a function call on our agent prompt.
- **Verbose reasoners can burn their entire output budget on thinking.** We add output headroom (`content_budget + 16384`, capped at 32768) and report the content-vs-reasoning split so this is visible. `doubao-seed-2.1-turbo` was the worst offender — random 200–8000+ thinking tokens, occasionally zero answer.
- **Effort tier doesn't mean the same thing across providers.** `high` thinking length varies wildly; always read the thinking-token column, not just the tier label.
- **Shared gateways degrade under long-context load.** We observed one OpenAI-compatible router whose TTFT grew from ~2s to ~15s across a cache sweep on 8k+ prefixes — a real property of the gateway, not the model.
- **Same model, different serving path = meaningfully different TPS.** Official-direct was ~27–33% faster in decode TPS than the hosted/plan path for both DeepSeek-V4-Flash and MiniMax-M3 in our testing. Worth measuring for your own providers.

## Interpreting a report

Don't rank models by one composite score. A model can win on throughput but lose badly on responsiveness:

- **Throughput-first** (80% decode TPS + 20% first-content TTFT) → long-form / batch generation.
- **Responsiveness-first** (80% first-content TTFT + 20% decode TPS) → interactive agents, where time-to-first-answer dominates.

The report shows both scores and both underlying columns. Pick the column that matches your workload.

## Scope and limitations

- Results are **point-in-time** — providers change models weekly. Treat the *method* as reusable and the *numbers* as a dated snapshot (see `examples/`).
- We test over the OpenAI **Responses API** only. Chat Completions endpoints may behave differently.
- Network is from the benchmark host; absolute TTFT includes that host's latency. Relative comparisons are what matter.
