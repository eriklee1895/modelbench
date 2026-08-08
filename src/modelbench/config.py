"""Config loading: endpoints from config.yaml, API keys from env (never from disk)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Endpoint:
    name: str
    group: str  # official | volc
    vendor: str
    base_url: str
    env_key: str
    models: list[str]

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.env_key)


@dataclass
class Defaults:
    repeats: int = 10
    warmup: int = 1
    timeout_s: float = 180.0
    temperature: float = 0.0
    max_output_tokens_cap: int = 4096
    model_concurrency: int = 3  # concurrent models within one endpoint


@dataclass
class Config:
    defaults: Defaults
    endpoints: list[Endpoint]

    def resolved_endpoints(self) -> list[Endpoint]:
        """Endpoints that have an API key available; others are reported as skipped."""
        return [e for e in self.endpoints if e.api_key]

    def skipped_endpoints(self) -> list[tuple[Endpoint, str]]:
        return [(e, f"missing env {e.env_key}") for e in self.endpoints if not e.api_key]


def load_config(path: str | Path) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)
    d = raw.get("defaults", {})
    defaults = Defaults(
        repeats=d.get("repeats", 10),
        warmup=d.get("warmup", 1),
        timeout_s=d.get("timeout_s", 180.0),
        temperature=d.get("temperature", 0.0),
        max_output_tokens_cap=d.get("max_output_tokens_cap", 4096),
        model_concurrency=d.get("model_concurrency", 3),
    )
    endpoints = [
        Endpoint(
            name=e["name"],
            group=e["group"],
            vendor=e["vendor"],
            base_url=e["base_url"].rstrip("/"),
            env_key=e["env_key"],
            models=list(e["models"]),
        )
        for e in raw["endpoints"]
    ]
    return Config(defaults=defaults, endpoints=endpoints)


def load_env_file(path: str | Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, ignores blank/non-KV lines.

    Does not override already-set env vars.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v
