"""Orchestration: probe endpoints, run the matrix, stream results to JSONL.

Design choices:
- Sequential per endpoint (no concurrency stress in this round) so TTFT/TPS are
  measured against an idle backend, comparable across models.
- Results are appended to JSONL after every run, so an interrupted run keeps its
  data and can be aggregated later.
- The `cache` case sends the identical request `repeats` times in a row; attempts
  2..N reveal context caching via cached_tokens and falling TTFT.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

from openai import AsyncOpenAI

from .config import Config, Endpoint
from .models import RunResult
from .runner import run_once
from .workloads import Workload


async def probe_endpoint(endpoint: Endpoint, request_timeout_s: float) -> dict[str, str | None]:
    """Minimal Responses call per model to confirm the endpoint/model pair works.

    Returns {model: None} if ok, {model: reason} if not.
    """
    out: dict[str, str | None] = {}
    client = AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key, timeout=request_timeout_s, max_retries=0)
    for model in endpoint.models:
        try:
            stream = await client.responses.create(
                model=model, input="ping", max_output_tokens=16, stream=True
            )
            async for _ in stream:
                pass
            out[model] = None
        except Exception as exc:
            out[model] = f"{type(exc).__name__}: {exc}"[:200]
    return out


async def run_matrix(
    config: Config,
    workloads: list[Workload],
    out_path: Path,
    only_endpoints: list[str] | None = None,
    only_models: list[str] | None = None,
    only_cases: list[str] | None = None,
    probe: bool = True,
    skip_models: set[str] | None = None,
) -> None:
    endpoints = config.resolved_endpoints()
    skipped = config.skipped_endpoints()
    for ep, reason in skipped:
        print(f"[skip] {ep.name}: {reason}")

    if only_endpoints:
        endpoints = [e for e in endpoints if e.name in only_endpoints]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fp = out_path.open("a", buffering=1)  # line-buffered
    write_lock = asyncio.Lock()

    async def emit(r: RunResult) -> None:
        r.derive()
        async with write_lock:
            fp.write(json.dumps(r.to_row(), ensure_ascii=False) + "\n")

    # probe phase (parallel across endpoints)
    probe_results: dict[str, dict[str, str | None]] = {}
    if probe:
        print("\n=== Probing endpoints ===")

        async def _probe(ep: Endpoint) -> None:
            probe_results[ep.name] = await probe_endpoint(ep, config.defaults.timeout_s)

        await asyncio.gather(*(_probe(ep) for ep in endpoints))
        for ep in endpoints:
            for m, err in probe_results[ep.name].items():
                tag = "ok" if err is None else f"FAIL ({err})"
                print(f"  {ep.name:18s} {m:28s} {tag}")

    cases = only_cases or [w.case for w in workloads]
    skip_models: set[str] = set(skip_models or [])

    async def run_model(ep: Endpoint, model: str) -> None:
        """Run one model's full case×repeat matrix serially. Serial within a model
        keeps per-request timing clean (no same-model self-contention)."""
        if only_models and model not in only_models:
            return
        if f"{ep.name}/{model}" in skip_models:
            print(f"[resume-skip] {ep.name}/{model}: already complete")
            return
        if probe and probe_results.get(ep.name, {}).get(model) is not None:
            print(f"[skip-model] {ep.name}/{model}: probe failed")
            return
        fresh = {w.case: copy.deepcopy(w) for w in workloads}
        for _ in range(config.defaults.warmup):
            w = fresh.get("S") or next(iter(fresh.values()))
            w.model = model
            await run_once(ep, w, config.defaults, rep=-1)
        for case in cases:
            w = fresh[case]
            w.model = model
            for rep in range(config.defaults.repeats):
                r = await run_once(ep, w, config.defaults, rep=rep)
                await emit(r)
                status = "ok" if r.success else f"ERR:{r.error_type}"
                ttft = f"{(r.ttft_content_s or r.ttft_s or 0) * 1000:.0f}ms" if (r.ttft_content_s or r.ttft_s) else "-"
                tps = f"{r.output_tps:.1f}" if r.output_tps else "-"
                print(f"  {ep.name:14s} {model:24s} {case:5s} rep{rep} {status:14s} ttft={ttft:>8s} tps={tps:>7s}")

    async def run_endpoint(ep: Endpoint) -> None:
        """Run an endpoint's models. Distinct models map to distinct backend
        deployments, so they can run concurrently; repeats within a model stay serial.
        A small semaphore bounds per-endpoint concurrency to avoid tripping per-key
        rate limits (which would distort TTFT as queueing delay)."""
        sem = asyncio.Semaphore(config.defaults.model_concurrency)

        async def _guarded(m: str) -> None:
            async with sem:
                await run_model(ep, m)

        await asyncio.gather(*(_guarded(m) for m in ep.models))

    print("\n=== Running matrix (parallel across endpoints & models, serial repeats) ===")
    await asyncio.gather(*(run_endpoint(ep) for ep in endpoints))
    fp.close()
    print(f"\nWrote {out_path}")


def read_results(path: Path) -> list[RunResult]:
    rows: list[RunResult] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(RunResult.from_row(json.loads(line)))
    return rows


async def effort_sweep(
    config: Config,
    base_workload: Workload,
    efforts: list[str],
    out_path: Path,
    repeats: int,
    only_models: list[str] | None = None,
    reasoning_models: dict[str, list[str]] | None = None,
) -> None:
    """Sweep reasoning effort tiers for each reasoning model on one workload.

    Each (model, effort) result is tagged with case=f"{base}@{effort}" so it
    aggregates separately from the default-effort runs. effort=None is included as
    the provider-default baseline (case=f"{base}@default").
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fp = out_path.open("a", buffering=1)
    lock = asyncio.Lock()

    async def emit(r: RunResult) -> None:
        r.derive()
        async with lock:
            fp.write(json.dumps(r.to_row(), ensure_ascii=False) + "\n")

    tiers = ["default", *efforts]  # default = no reasoning param

    async def run_one(ep: Endpoint, model: str) -> None:
        for eff in tiers:
            w = copy.deepcopy(base_workload)
            w.model = model
            w.effort = None if eff == "default" else eff
            w.case = f"{base_workload.case}@{eff}"
            for rep in range(repeats):
                r = await run_once(ep, w, config.defaults, rep=rep)
                await emit(r)
                rr = (r.usage.output_tokens or 0) - (r.usage.content_tokens or 0)
                ttft = (r.ttft_content_s or r.ttft_s or 0) * 1000
                print(
                    f"  {model:22s} {w.case:12s} rep{rep} "
                    f"{'ok' if r.success else 'ERR'} reasoning={rr} content={r.usage.content_tokens} "
                    f"ttft_content={ttft:.0f}ms tps={r.output_tps and round(r.output_tps, 1)}"
                )

    # group reasoning models by endpoint
    for ep in config.resolved_endpoints():
        models = (reasoning_models or {}).get(ep.name, ep.models)
        if only_models:
            models = [m for m in models if m in only_models]
        sem = asyncio.Semaphore(config.defaults.model_concurrency)

        async def _g(m: str, ep: Endpoint = ep, sem: asyncio.Semaphore = sem) -> None:
            async with sem:
                await run_one(ep, m)

        await asyncio.gather(*(_g(m) for m in models))
    fp.close()
    print(f"\nWrote {out_path}")


def completed_models(path: Path, n_cases: int = 6, min_rows: int = 8) -> set[str]:
    """Models that already have all n_cases (each with >=min_rows) in an existing
    results file. Used to resume a partial run without re-measuring finished models."""
    if not path.exists():
        return set()
    from collections import defaultdict

    cases: dict[str, set[str]] = defaultdict(set)
    rows: dict[tuple[str, str], int] = defaultdict(int)
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("rep", 0) < 0:
                continue
            key = f"{d['endpoint']}/{d['model']}"
            cases[key].add(d["case"])
            rows[(key, d["case"])] += 1
    done = set()
    for key, cs in cases.items():
        if len(cs) >= n_cases and all(rows[(key, c)] >= min_rows for c in cs):
            done.add(key)
    return done


def new_results_path(results_dir: Path) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return results_dir / f"raw_{ts}.jsonl"
