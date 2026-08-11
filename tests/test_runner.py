"""Unit tests for streaming timing boundaries (no network)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import tiktoken

import modelbench.runner as runner
from modelbench.config import Defaults, Endpoint
from modelbench.models import RunResult
from modelbench.workloads import Workload


class _FakeStreamClient:
    def __init__(
        self,
        events: list[object],
        fail_first: bool = False,
        first_error: BaseException | None = None,
        empty_first: bool = False,
    ) -> None:
        self.events = events
        self.fail_first = fail_first
        self.first_error = first_error
        self.empty_first = empty_first
        self.calls = 0
        self.requests: list[dict] = []
        self.responses = self

    async def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.fail_first and self.calls == 1:
            raise self.first_error or TimeoutError("first attempt")

        events = self.events
        if self.empty_first and self.calls == 1:
            events = [SimpleNamespace(type="response.completed", response=_completed(""))]

        async def stream():
            for event in events:
                yield event

        return stream()


def _endpoint() -> Endpoint:
    return Endpoint(
        name="ep",
        group="test",
        vendor="test",
        base_url="https://example.invalid/v1",
        env_key="MISSING_TEST_KEY",
        models=["m"],
    )


def _workload() -> Workload:
    workload = Workload(case="S", input_text="prompt", max_output_tokens=20, instructions="")
    workload.model = "m"
    return workload


def _completed(text: str) -> dict:
    encoding = tiktoken.get_encoding("cl100k_base")
    return {
        "status": "completed",
        "usage": {
            "input_tokens": 1,
            "output_tokens": len(encoding.encode(text)),
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def test_stream_timing_ignores_empty_delta_and_excludes_first_chunk(monkeypatch):
    text = "The answer is useful."
    first = "The answer "
    second = "is useful."
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=""),
        SimpleNamespace(type="response.output_text.delta", delta=first),
        SimpleNamespace(type="response.output_text.delta", delta=second),
        SimpleNamespace(type="response.completed", response=_completed(text)),
    ]
    client = _FakeStreamClient(events)
    times = iter([10.0, 10.1, 10.2, 10.7, 10.8])
    monkeypatch.setattr(runner.time, "perf_counter", lambda: next(times))

    result = asyncio.run(run_once_for_test(client))

    encoding = tiktoken.get_encoding("cl100k_base")
    total_tokens = len(encoding.encode(text))
    first_tokens = len(encoding.encode(first))
    assert result.ttft_s == pytest.approx(0.2)
    assert result.ttft_content_s == pytest.approx(0.2)
    assert result.decode_window_s == pytest.approx(0.5)
    assert result.content_chunk_tokens == total_tokens
    assert result.first_content_chunk_tokens == first_tokens
    assert result.decode_tps == pytest.approx((total_tokens - first_tokens) / 0.5)


def test_retry_delay_is_outside_timing_window(monkeypatch):
    text = "retry works"
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(type="response.completed", response=_completed(text)),
    ]
    client = _FakeStreamClient(events, fail_first=True)
    times = iter([20.0, 21.0, 21.1, 21.2])
    monkeypatch.setattr(runner.time, "perf_counter", lambda: next(times))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    result = asyncio.run(run_once_for_test(client))

    assert result.success is True
    assert result.retried is True
    assert result.ttft_s == pytest.approx(0.1)
    assert client.calls == 2


def test_transient_gateway_error_is_retried(monkeypatch):
    text = "gateway recovered"
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(type="response.completed", response=_completed(text)),
    ]
    client = _FakeStreamClient(
        events,
        fail_first=True,
        first_error=RuntimeError("Our servers are currently overloaded"),
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    result = asyncio.run(run_once_for_test(client))

    assert result.success is True
    assert result.retried is True
    assert client.calls == 2


def test_empty_response_is_retried(monkeypatch):
    text = "empty once"
    events = [
        SimpleNamespace(type="response.output_text.delta", delta=text),
        SimpleNamespace(type="response.completed", response=_completed(text)),
    ]
    client = _FakeStreamClient(events, empty_first=True)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(runner.asyncio, "sleep", no_sleep)
    result = asyncio.run(run_once_for_test(client))

    assert result.success is True
    assert result.retried is True
    assert client.calls == 2


def test_permanent_other_error_is_not_retried(monkeypatch):
    client = _FakeStreamClient(
        [],
        fail_first=True,
        first_error=RuntimeError("invalid parameter: unsupported option"),
    )
    result = asyncio.run(run_once_for_test(client))

    assert result.success is False
    assert result.error_type == "other"
    assert result.retried is False
    assert client.calls == 1


async def run_once_for_test(client: _FakeStreamClient) -> RunResult:
    return await runner.run_once(
        _endpoint(),
        _workload(),
        Defaults(timeout_s=5.0, max_output_tokens_cap=100),
        rep=0,
        client=client,
    )
