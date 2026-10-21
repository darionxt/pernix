from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pernix import Spider, events
from pernix.pausing import PauseCounts, Pausing

if TYPE_CHECKING:
    import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _pausing(
    clock: Clock | None = None, bus: events.EventBus | None = None
) -> tuple[Pausing, Spider, list[events.Event]]:
    """A Pausing whose Events are recorded, first of their subscribers."""
    spider, bus = Spider(), bus or events.EventBus(timeout=5)
    recorded: list[events.Event] = []
    bus.on(events.SpiderPaused, recorded.append)
    bus.on(events.SpiderResumed, recorded.append)
    return Pausing(spider, bus, clock=clock or Clock()), spider, recorded


async def _announced(pausing: Pausing) -> None:
    """End the Pausing and emit what it queued."""
    pausing.end()
    async with asyncio.timeout(5):
        await pausing.announce()


async def test_pauses_and_resumes_are_announced_in_the_order_requested(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pernix")
    clock = Clock()
    pausing, spider, recorded = _pausing(clock)
    pausing.begin()

    pausing.pause()
    clock.now += 2.5
    pausing.resume()
    pausing.pause()
    await _announced(pausing)

    assert recorded == [events.SpiderPaused(spider), events.SpiderResumed(spider, 2.5), events.SpiderPaused(spider)]
    assert "Pausing the Run: no new Request is claimed until it is resumed" in caplog.text
    assert "Resuming the Run after 2.5 s paused" in caplog.text


async def test_a_request_that_changes_nothing_does_nothing() -> None:
    pausing, spider, recorded = _pausing()
    pausing.begin()

    pausing.resume()
    pausing.pause()
    pausing.pause()
    await _announced(pausing)

    assert recorded == [events.SpiderPaused(spider)]
    assert pausing.counts().pauses == 1


async def test_a_pause_holds_the_run_at_once_and_a_resume_releases_it_once_announced() -> None:
    bus = events.EventBus(timeout=5)
    pausing, _, _ = _pausing(bus=bus)
    held_while_announced: list[bool] = []
    bus.on(events.SpiderResumed, lambda _event: held_while_announced.append(pausing.held))
    pausing.begin()
    announcing = asyncio.create_task(pausing.announce())

    pausing.pause()
    assert (pausing.paused, pausing.held) == (True, True)
    pausing.resume()
    assert (pausing.paused, pausing.held) == (False, True)
    async with asyncio.timeout(5):
        await pausing.released()

    assert held_while_announced == [True]
    pausing.end()
    await announcing


async def test_waiting_for_a_hold_returns_once_paused_and_at_once_while_held() -> None:
    pausing, _, _ = _pausing()
    pausing.begin()
    waiting = asyncio.create_task(pausing.holding())
    await asyncio.sleep(0)
    before = waiting.done()

    pausing.pause()
    async with asyncio.timeout(5):
        await waiting
        await pausing.holding()

    assert not before
    await _announced(pausing)


async def test_a_run_paused_again_before_its_resume_is_announced_stays_held() -> None:
    bus, announced = events.EventBus(timeout=5), asyncio.Event()
    pausing, spider, recorded = _pausing(bus=bus)
    bus.on(events.SpiderPaused, lambda _event: announced.set() if len(recorded) == 3 else None)
    pausing.begin()

    pausing.pause()
    pausing.resume()
    pausing.pause()
    announcing = asyncio.create_task(pausing.announce())
    async with asyncio.timeout(5):
        await announced.wait()

    assert recorded == [events.SpiderPaused(spider), events.SpiderResumed(spider, 0.0), events.SpiderPaused(spider)]
    assert pausing.held
    pausing.end()
    await announcing


async def test_a_pause_requested_before_it_begins_takes_effect_then() -> None:
    clock = Clock()
    pausing, spider, recorded = _pausing(clock)

    pausing.pause()
    clock.now += 7  # before the Run, the time is not counted
    assert pausing.paused
    assert pausing.counts() == PauseCounts(pauses=0, paused_seconds=0.0)
    pausing.begin()
    clock.now += 1
    await _announced(pausing)

    assert recorded == [events.SpiderPaused(spider)]
    assert pausing.counts() == PauseCounts(pauses=1, paused_seconds=1.0)


async def test_a_pause_resumed_before_it_begins_is_not_announced() -> None:
    pausing, _, recorded = _pausing()

    pausing.pause()
    pausing.resume()
    assert not pausing.held
    pausing.begin()
    await _announced(pausing)

    assert recorded == []
    assert pausing.counts() == PauseCounts(pauses=0, paused_seconds=0.0)


async def test_the_time_paused_includes_the_pause_in_progress() -> None:
    clock = Clock()
    pausing, _, _ = _pausing(clock)
    pausing.begin()

    pausing.pause()
    clock.now += 1.25
    pausing.resume()
    clock.now += 10
    pausing.pause()
    clock.now += 0.5

    assert pausing.counts() == PauseCounts(pauses=2, paused_seconds=1.75)
    pausing.end()


async def test_ending_stops_the_clock_releases_the_waits_and_ignores_later_requests() -> None:
    clock = Clock()
    pausing, spider, recorded = _pausing(clock)
    pausing.begin()
    pausing.pause()
    waiting = asyncio.create_task(pausing.released())
    clock.now += 3

    await _announced(pausing)
    clock.now += 10
    pausing.pause()
    pausing.resume()
    async with asyncio.timeout(5):
        await waiting

    assert (pausing.paused, pausing.held) == (False, False)
    assert recorded == [events.SpiderPaused(spider)]
    assert pausing.counts() == PauseCounts(pauses=1, paused_seconds=3.0)


async def test_ending_before_it_begins_keeps_it_from_beginning() -> None:
    pausing, _, recorded = _pausing()
    pausing.pause()

    pausing.end()
    pausing.begin()
    await _announced(pausing)

    assert not pausing.paused
    assert recorded == []
