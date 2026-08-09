"""CLI entrypoint.

Usage:
    uv run python -m modelbench.cli run      [--config config.yaml] [--env .env]
                                             [--endpoints a,b] [--models x,y] [--cases S,M]
                                             [--repeats N] [--resume] [--no-probe]
    uv run python -m modelbench.cli effort   [--case M] [--efforts low,medium,high]
    uv run python -m modelbench.cli report   --results results/raw_<ts>.jsonl [--effort results/effort.jsonl]
    uv run python -m modelbench.cli probe    # endpoint availability only
"""

from __future__ import annotations

import argparse
import asyncio
from copy import replace
from pathlib import Path

from .config import load_config, load_env_file
from .engine import completed_models, new_results_path, probe_endpoint, run_matrix
from .report import write_report
from .workloads import all_workloads

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_env(env_path: str | None) -> None:
    # priority: explicit --env > ./.env > ./env > ./.model_accounts.
    # Keys can also just be exported in the shell. All these filenames are
    # git-ignored; the convention is generic (no project-specific paths).
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    candidates += [
        ROOT / ".env",
        ROOT / "env",
        ROOT / ".model_accounts",
    ]
    for c in candidates:
        if c.exists():
            load_env_file(c)
            print(f"[env] loaded {c}")
            return
    print("[env] no env file found; relying on exported env vars")


def _resolve_config(path: str | None) -> Path:
    if path:
        return Path(path)
    for cand in (ROOT / "config.yaml", ROOT / "config.example.yaml"):
        if cand.exists():
            return cand
    return ROOT / "config.yaml"  # let load_config raise a clear error


def cmd_run(args: argparse.Namespace) -> None:
    _load_env(args.env)
    config = load_config(_resolve_config(args.config))
    if args.repeats:
        config.defaults.repeats = args.repeats
    out_path = Path(args.out) if args.out else new_results_path(ROOT / "results")
    skip: set[str] = set()
    if args.resume:
        skip = completed_models(out_path)
        if skip:
            print(f"[resume] {len(skip)} models already complete, skipping")
    workloads = all_workloads()
    asyncio.run(
        run_matrix(
            config,
            workloads,
            out_path,
            only_endpoints=args.endpoints.split(",") if args.endpoints else None,
            only_models=args.models.split(",") if args.models else None,
            only_cases=args.cases.split(",") if args.cases else None,
            probe=not args.no_probe,
            skip_models=skip,
        )
    )
    if args.report:
        rp = write_report(out_path, ROOT / "results")
        print(f"[report] {rp}")


def cmd_report(args: argparse.Namespace) -> None:
    effort = Path(args.effort) if args.effort else None
    rp = write_report(Path(args.results), Path(args.out_dir or ROOT / "results"), effort_path=effort)
    print(f"[report] {rp}")


def cmd_probe(args: argparse.Namespace) -> None:
    _load_env(args.env)
    config = load_config(_resolve_config(args.config))
    only_eps = set(args.endpoints.split(",")) if args.endpoints else None
    only_models = set(args.models.split(",")) if args.models else None

    async def _go() -> None:
        for ep in config.endpoints:
            if only_eps and ep.name not in only_eps:
                continue
            if not ep.api_key:
                print(f"[skip] {ep.name}: missing env {ep.env_key}")
                continue
            models = [m for m in ep.models if not only_models or m in only_models]
            if not models:
                continue
            sub = replace(ep, models=models)
            res = await probe_endpoint(sub, config.defaults.timeout_s)
            for m, err in res.items():
                tag = "ok" if err is None else f"FAIL ({err})"
                print(f"  {ep.name:18s} {m:28s} {tag}")

    asyncio.run(_go())


def cmd_effort(args: argparse.Namespace) -> None:
    _load_env(args.env)
    config = load_config(_resolve_config(args.config))
    out_path = Path(args.out) if args.out else new_results_path(ROOT / "results")
    base = next(w for w in all_workloads() if w.case == args.case)
    from .engine import effort_sweep

    # By default sweep every model in the config; models that don't accept a
    # `reasoning` param simply record default-tier behaviour. --models narrows it.
    asyncio.run(
        effort_sweep(
            config,
            base,
            efforts=args.efforts.split(","),
            out_path=out_path,
            repeats=args.repeats,
            only_models=args.models.split(",") if args.models else None,
            reasoning_models=None,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser(prog="modelbench")
    ap.add_argument("--config", default=None, help="config.yaml path (default: ./config.yaml, falls back to config.example.yaml)")
    ap.add_argument("--env", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run the benchmark")
    pr.add_argument("--endpoints", default=None, help="comma-separated endpoint names")
    pr.add_argument("--models", default=None, help="comma-separated model ids")
    pr.add_argument("--cases", default=None, help="comma-separated cases (S,M,L,XL,agent,cache)")
    pr.add_argument("--repeats", type=int, default=None)
    pr.add_argument("--out", default=None)
    pr.add_argument("--no-probe", action="store_true")
    pr.add_argument("--resume", action="store_true", help="skip models already complete in --out file")
    pr.add_argument("--report", action="store_true", help="also write report after run")
    pr.set_defaults(fn=cmd_run)

    pp = sub.add_parser("report", help="aggregate + report from a results JSONL")
    pp.add_argument("--results", required=True)
    pp.add_argument("--out-dir", default=None)
    pp.add_argument("--effort", default=None, help="optional effort-sweep JSONL to fold into the report")
    pp.set_defaults(fn=cmd_report)

    pj = sub.add_parser("probe", help="probe endpoint/model availability only")
    pj.add_argument("--endpoints", default=None, help="comma-separated endpoint names")
    pj.add_argument("--models", default=None, help="comma-separated model ids")
    pj.set_defaults(fn=cmd_probe)

    pe = sub.add_parser("effort", help="sweep reasoning effort tiers (low/medium/high)")
    pe.add_argument("--case", default="M", help="base workload case (default M)")
    pe.add_argument("--efforts", default="low,medium,high")
    pe.add_argument("--models", default=None, help="comma-separated model ids (default: all reasoning models)")
    pe.add_argument("--repeats", type=int, default=3)
    pe.add_argument("--out", default=None)
    pe.set_defaults(fn=cmd_effort)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
