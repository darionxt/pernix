from __future__ import annotations

import asyncio
import logging
from typing import Self

import pytest

from pernix.component_lifecycle import ComponentLifecycle


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class AsyncComponent:
    def __init__(self, name: str, recorder: Recorder, *, fail_open: bool = False, fail_close: bool = False) -> None:
        self.name, self.recorder, self.fail_open, self.fail_close = name, recorder, fail_open, fail_close

    async def open(self) -> None:
        self.recorder.calls.append(f"open {self.name}")
        if self.fail_open:
            raise RuntimeError(f"cannot open {self.name}")

    async def close(self) -> None:
        self.recorder.calls.append(f"close {self.name}")
        if self.fail_close:
            raise RuntimeError(f"cannot close {self.name}")


class SyncComponent:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def open(self) -> None:
        self.recorder.calls.append("open sync")

    def close(self) -> None:
        self.recorder.calls.append("close sync")


class ContextManagerComponent:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def __aenter__(self) -> Self:
        self.recorder.calls.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.recorder.calls.append("exit")


async def test_components_open_in_order_and_close_in_reverse() -> None:
    recorder = Recorder()

    async with ComponentLifecycle() as lifecycle:
        await lifecycle.open(AsyncComponent("a", recorder))
        await lifecycle.open(SyncComponent(recorder))
        await lifecycle.open(ContextManagerComponent(recorder))
        await lifecycle.open(object())
        recorder.calls.append("run")

    assert recorder.calls == ["open a", "open sync", "enter", "run", "exit", "close sync", "close a"]


async def test_startup_failure_closes_the_opened_components_in_reverse() -> None:
    recorder = Recorder()

    async def start() -> None:
        async with ComponentLifecycle() as lifecycle:
            for name in ("a", "b", "c", "d"):
                await lifecycle.open(AsyncComponent(name, recorder, fail_open=name == "c"))

    with pytest.raises(RuntimeError, match="cannot open c"):
        await start()

    assert recorder.calls == ["open a", "open b", "open c", "close b", "close a"]


async def test_every_close_step_runs_and_failures_are_grouped() -> None:
    recorder = Recorder()

    async def run() -> None:
        async with ComponentLifecycle() as lifecycle:
            for name in ("a", "b", "c"):
                await lifecycle.open(AsyncComponent(name, recorder, fail_close=name != "b"))

    with pytest.raises(ExceptionGroup) as excinfo:
        await run()

    assert recorder.calls[-3:] == ["close c", "close b", "close a"]
    assert [str(error) for error in excinfo.value.exceptions] == ["cannot close c", "cannot close a"]
    assert "while closing" in excinfo.value.exceptions[0].__notes__[0]


async def test_a_failed_run_and_failed_closes_are_reported_together() -> None:
    recorder = Recorder()

    async def run() -> None:
        async with ComponentLifecycle() as lifecycle:
            await lifecycle.open(AsyncComponent("a", recorder, fail_close=True))
            raise ValueError("run failed")

    with pytest.raises(ExceptionGroup) as excinfo:
        await run()

    assert [type(error) for error in excinfo.value.exceptions] == [ValueError, RuntimeError]


async def test_cancellation_is_not_replaced_by_close_failures(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    opened = asyncio.Event()

    async def run() -> None:
        async with ComponentLifecycle() as lifecycle:
            await lifecycle.open(AsyncComponent("a", recorder, fail_close=True))
            opened.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(run())
    await opened.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.calls == ["open a", "close a"]
    assert any(r.levelno == logging.ERROR and "during CancelledError" in r.getMessage() for r in caplog.records)


class TransactionComponent:
    """An async context manager that records the exception it was closed with."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> bool:
        self.recorder.calls.append("commit" if exc is None else f"rollback {type(exc).__name__}")
        return True  # must not suppress the Run's exception


class CancelledOnClose:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def close(self) -> None:
        self.recorder.calls.append("close cancelled")
        raise asyncio.CancelledError


async def test_context_managers_receive_the_exception_that_ended_the_run() -> None:
    recorder = Recorder()

    async def run() -> None:
        async with ComponentLifecycle() as lifecycle:
            await lifecycle.open(TransactionComponent(recorder))
            raise ValueError("run failed")

    with pytest.raises(ValueError, match="run failed"):
        await run()
    async with ComponentLifecycle() as lifecycle:
        await lifecycle.open(TransactionComponent(recorder))

    assert recorder.calls == ["rollback ValueError", "commit"]


async def test_context_managers_see_a_later_component_failing_to_open() -> None:
    recorder = Recorder()

    async def start() -> None:
        async with ComponentLifecycle() as lifecycle:
            await lifecycle.open(TransactionComponent(recorder))
            await lifecycle.open(AsyncComponent("b", recorder, fail_open=True))

    with pytest.raises(RuntimeError, match="cannot open b"):
        await start()
    assert recorder.calls == ["open b", "rollback RuntimeError"]


async def test_a_cancelled_close_step_does_not_skip_the_others(caplog: pytest.LogCaptureFixture) -> None:
    recorder = Recorder()
    lifecycle = ComponentLifecycle()
    await lifecycle.open(AsyncComponent("a", recorder))
    await lifecycle.open(AsyncComponent("b", recorder, fail_close=True))
    await lifecycle.open(CancelledOnClose(recorder))

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.aclose()

    assert recorder.calls == ["open a", "open b", "close cancelled", "close b", "close a"]
    assert any("Closing components failed during CancelledError" in r.getMessage() for r in caplog.records)


async def test_cancellation_during_cleanup_still_closes_every_component() -> None:
    recorder = Recorder()
    started, release = asyncio.Event(), asyncio.Event()

    class SlowClose:
        async def close(self) -> None:
            recorder.calls.append("slow close started")
            started.set()
            await release.wait()

    lifecycle = ComponentLifecycle()
    await lifecycle.open(AsyncComponent("a", recorder))
    await lifecycle.open(SlowClose())
    task = asyncio.create_task(lifecycle.aclose())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.calls == ["open a", "slow close started", "close a"]
