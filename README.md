# modelbench

A reasoning-aware LLM performance benchmark over the **OpenAI Responses API**.

English | [中文](README_CN.md) · [Methodology](METHODOLOGY.md)

Measures **TTFT, decode TPS, end-to-end latency, reliability, prompt-caching effect, and reasoning-effort tiers** across any Responses-compatible endpoint — official provider APIs, hosted plans, and routers/gateways — with the same workload, so numbers are comparable.

## Why this exists

Most LLM latency tools report a single "tokens/sec" and a single "TTFT". That breaks down for **reasoning models** (DeepSeek, Kimi, GLM, Doubao, …), which emit a long hidden *thinking* stream before the visible answer. A naive benchmark either:

- counts thinking tokens as output (inflating TPS), or
- measures TTFT to the first *thinking* token (hiding the real time-to-answer), or
- silently fails when a provider doesn't return `usage` in streaming mode (DeepSeek official doesn't).

modelbench is built around three measurement choices that fix this:

1. **Chunk-timing decode TPS** — `content_tokens / (last_content_chunk − first_content_chunk)`. The decode window starts at the first *visible* token by construction, so it's immune to both missing `usage` and thinking-length variance.
2. **Split TTFT** — *first-response TTFT* (model starts thinking) vs *first-content TTFT* (model starts answering). For an agent, the second is what users feel.
3. **Reasoning-effort sweep** — runs `low / medium / high` tiers per model to show the latency cost of "thinking harder" (and that decode TPS barely changes — the cost is all in TTFT).

It also does a **data-quality audit** after each run (truncation, empty responses, token-estimation fallbacks, variance) so you can tell whether a number is trustworthy before you quote it.

## Metrics

| Metric | Definition |
|---|---|
| First-response TTFT | time to first output event (first *thinking* token for reasoning models) |
| First-content TTFT | time to first *visible answer* token |
| **Decode TPS** | content tokens / (last−first content chunk) — pure decode rate |
| End-to-end TPS | content tokens / total time — closest to perceived speed |
| E2E latency | request → `response.completed` |
| Tool-call latency | time to first `function_call` item (agent workload) |
| Tool-call validity | % of agent calls emitting a schema-parseable function call |
| Prompt-caching | `cached_tokens` + first-response TTFT drop across repeated identical prefixes |

## Install

```bash
git clone <this-repo> && cd modelbench
uv sync
```

## Configure

```bash
cp config.example.yaml config.yaml   # add your endpoints + model ids
cp .env.example .env                 # add your API keys
```

`config.yaml` and `.env` are git-ignored. Keys are read from env vars, never from disk-committed files.

## Run

```bash
# probe endpoint/model availability first (cheap)
uv run python -m modelbench.cli probe

# full benchmark (S/M/L/XL + agent + cache workloads), then a report
uv run python -m modelbench.cli run --report

# quick smoke on a subset
uv run python -m modelbench.cli run --endpoints hosted-plan --models glm-5.2 --cases S,M --repeats 3 --report

# reasoning-effort sweep (low/medium/high)
uv run python -m modelbench.cli effort --case M --efforts low,medium,high

# report from an existing results file
uv run python -m modelbench.cli report --results results/raw_<ts>.jsonl --effort results/effort.jsonl

# audit data quality of a results file
uv run python -m modelbench.audit results/raw_<ts>.jsonl
```

## Quickstart: benchmarking a new model

The most common task — measure TTFT/TPS for one new model:

```bash
# 1. Add the model id under the right endpoint in config.yaml
# 2. Probe it (cheap, confirms the endpoint/model works)
uv run python -m modelbench.cli probe --endpoints deepseek-official --models <new-model-id>

# 3. Quick measure (3 repeats × M case) to get a fast read
uv run python -m modelbench.cli run --endpoints deepseek-official --models <new-model-id> \
  --cases M --repeats 3 --report --out results/<new-model-id>.jsonl

# 4. Full benchmark (all cases × 10 repeats) when you want the real number
uv run python -m modelbench.cli run --endpoints deepseek-official --models <new-model-id> --report
```

Adding a **new provider** (not just a model)? Add an `endpoints:` block in `config.yaml` (see `config.example.yaml`), put its key in `.env` / `.model_accounts`, then `probe` → `run`.

### Which number matters

In the report, look at both:

| Column | What it tells you |
|---|---|
| **Decode TPS** | pure token emission rate (chunk-timed, excludes thinking) — the most stable, comparable number |
| **End-to-end TPS** | throughput over total time, including first-token latency — closest to perceived speed |
| **First-content TTFT** | time to the first *visible* answer token (for reasoning models: after thinking) — what users actually feel |

For **reasoning models**, first-response TTFT (start of thinking) and first-content TTFT (start of answer) differ a lot — don't conflate them.

See **[METHODOLOGY.md](METHODOLOGY.md)** for how metrics are computed and how to read the data-quality audit.

## Workloads

| case | input | output | purpose |
|---|---|---|---|
| S | ~50 tok | 200 | interactive |
| M | ~1k | 500 | typical agent step |
| L | ~4k | 1000 | long context |
| XL | ~8k | 2000 | stress long context |
| agent | ~2k + tools | 1 function call | tool-use latency + validity |
| cache | ~6k static prefix × N | 200 | prompt-caching effect |

## Output

`results/` holds `raw_<ts>.jsonl` (per-request detail), `report_<ts>.md` (aggregated tables + analysis), and PNG charts. See **[examples/](examples/)** for a real report.

## Methodology notes & caveats

- Requests within a (model, case) run **serially**; different models run concurrently (they hit distinct backends). A per-endpoint semaphore bounds concurrency to avoid tripping rate limits, which would otherwise show up as inflated TTFT.
- Reasoning models can burn their entire output budget on thinking, yielding empty answers; modelbench adds output headroom and reports content vs reasoning token split so this is visible, not silent.
- A single composite "speed score" is misleading (a model can win on throughput but take 50s to start answering). The report therefore scores **throughput-first** and **responsiveness-first** separately.
- Results are point-in-time and provider-side variable. Treat the *method* as reusable and the *numbers* as a dated snapshot.

## License

MIT
