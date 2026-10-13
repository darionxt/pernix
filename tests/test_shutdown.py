from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from pernix.shutdown import Shutdown

if TYPE_CHECKING:
    from pernix.component_lifecycle import ComponentLifecycle
    from pernix.engine import Engine


class FakeEngine:
    def __init__(self) -> None:
        self.drains = 0

    def drain(self) -> None:
        self.drains += 1


class FakeLifecycle:
    def __init__(self) -> None:
        self.limits: list[float | None] = []

    def limit_closing(self, timeout: float | None) -> None:
        self.limits.append(timeout)


def _shutdown(*, grace: float = 60) -> tuple[Shutdown, FakeEngine, FakeLifecycle]:
    shutdown = Shutdown(grace=grace, close_timeout=5)
    engine, lifecycle = FakeEngine(), FakeLifecycle()
    shutdown.attach(cast("Engine", engine))
    shutdown.begin(cast("ComponentLifecycle", lifecycle))
    return shutdown, engine, lifecycle


async def _forever() -> None:
    await asyncio.Event().wait()


async def test_a_drain_drains_the_engine_once() -> None:
    shutdown, engine, lifecycle = _shutdown()

    shutdown.request("drain")
    shutdown.request("drain")

    assert (shutdown.mode, engine.drains, lifecycle.limits) == ("drain", 1, [])
    shutdown.end()


async def test_a_force_stop_cancels_the_stage_in_progress_and_limits_closing() -> None:
    shutdown, engine, lifecycle = _shutdown()
    stage = asyncio.create_task(shutdown.run_stage(_forever()))
    await asyncio.sleep(0)

    shutdown.request("drain")
    shutdown.request("force")

    assert await stage is True
    assert (shutdown.mode, shutdown.forced, engine.drains, lifecycle.limits) == ("force", True, 1, [5])
    shutdown.request("drain")  # changes nothing during a Force Stop
    assert shutdown.mode == "force"


async def test_a_stage_after_a_force_stop_is_cancelled_at_once() -> None:
    shutdown, _, _ = _shutdown()
    shutdown.request("force")

    assert await shutdown.run_stage(_forever()) is True


async def test_a_stage_that_completes_was_not_forced() -> None:
    shutdown, _, _ = _shutdown()

    assert await shutdown.run_stage(asyncio.sleep(0)) is False


async def test_cancelling_the_caller_of_a_stage_is_not_a_force_stop() -> None:
    shutdown, _, _ = _shutdown()
    caller = asyncio.create_task(shutdown.run_stage(_forever()))
    await asyncio.sleep(0)

    shutdown.request("force")
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller


async def test_a_drain_past_its_grace_turns_into_a_force_stop(caplog: pytest.LogCaptureFixture) -> None:
    shutdown, _, lifecycle = _shutdown(grace=0.01)
    stage = asyncio.create_task(shutdown.run_stage(_forever()))

    shutdown.request("drain")
    async with asyncio.timeout(5):
        assert await stage is True

    assert lifecycle.limits == [5]
    assert "The Drain took longer than shutdown.grace (0.01 s)" in caplog.text


async def test_requests_made_before_the_run_apply_when_it_begins() -> None:
    shutdown = Shutdown(grace=60, close_timeout=None)
    engine, lifecycle = FakeEngine(), FakeLifecycle()

    shutdown.request("drain")
    shutdown.attach(cast("Engine", engine))
    assert engine.drains == 1
    shutdown.request("force")
    shutdown.begin(cast("ComponentLifecycle", lifecycle))

    assert lifecycle.limits == [None]
    shutdown.end()


async def test_requests_after_the_end_do_nothing() -> None:
    shutdown, engine, lifecycle = _shutdown(grace=0.01)
    shutdown.request("drain")
    shutdown.end()  # also cancels the grace period

    shutdown.request("force")
    await asyncio.sleep(0.05)

    assert (shutdown.mode, engine.drains, lifecycle.limits) == ("drain", 1, [])


def test_unknown_modes_are_rejected() -> None:
    with pytest.raises(ValueError, match="the stop mode must be 'drain' or 'force', got 'now'"):
        Shutdown(grace=1, close_timeout=None).request("now")  # pyright: ignore[reportArgumentType]
