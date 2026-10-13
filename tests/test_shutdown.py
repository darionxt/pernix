from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from pernix.shutdown import Shutdown

if TYPE_CHECKING:
    from pernix.engine import Engine


class FakeEngine:
    def __init__(self) -> None:
        self.drains = 0

    def drain(self) -> None:
        self.drains += 1


async def _work() -> None:
    await asyncio.Event().wait()


def _start(shutdown: Shutdown) -> tuple[FakeEngine, asyncio.Task[None]]:
    engine, work = FakeEngine(), asyncio.create_task(_work())
    shutdown.start(cast("Engine", engine), work)
    return engine, work


async def _finish(work: asyncio.Task[None]) -> None:
    work.cancel()
    await asyncio.wait([work])


async def test_a_drain_drains_the_engine_once() -> None:
    shutdown = Shutdown(grace=60)
    engine, work = _start(shutdown)

    shutdown.request("drain")
    shutdown.request("drain")
    await asyncio.sleep(0)

    assert (shutdown.mode, engine.drains, work.done()) == ("drain", 1, False)
    shutdown.end()
    await _finish(work)


async def test_a_force_stop_cancels_the_work_and_escalates_a_drain() -> None:
    shutdown = Shutdown(grace=60)
    engine, work = _start(shutdown)

    shutdown.request("drain")
    shutdown.request("force")
    await asyncio.wait([work])

    assert (shutdown.mode, shutdown.forced, engine.drains, work.cancelled()) == ("force", True, 1, True)
    shutdown.request("drain")  # changes nothing during a Force Stop
    assert shutdown.mode == "force"


async def test_a_drain_past_its_grace_turns_into_a_force_stop(caplog: pytest.LogCaptureFixture) -> None:
    shutdown = Shutdown(grace=0.01)
    _, work = _start(shutdown)

    shutdown.request("drain")
    async with asyncio.timeout(5):
        await asyncio.wait([work])

    assert shutdown.forced
    assert "The Drain took longer than shutdown.grace (0.01 s)" in caplog.text


async def test_a_stop_requested_before_the_work_starts_applies_when_it_does() -> None:
    shutdown = Shutdown(grace=60)

    shutdown.request("force")
    _, work = _start(shutdown)
    await asyncio.wait([work])

    assert work.cancelled()


async def test_requests_after_the_end_do_nothing() -> None:
    shutdown = Shutdown(grace=0.01)
    engine, work = _start(shutdown)
    shutdown.request("drain")
    shutdown.end()  # also cancels the grace period

    shutdown.request("force")
    await asyncio.sleep(0.05)

    assert (shutdown.mode, engine.drains, work.done()) == ("drain", 1, False)
    await _finish(work)


def test_unknown_modes_are_rejected() -> None:
    with pytest.raises(ValueError, match="the stop mode must be 'drain' or 'force', got 'now'"):
        Shutdown(grace=1).request("now")  # pyright: ignore[reportArgumentType]
