from __future__ import annotations

import asyncio
import time

import pytest

from pernix.fetchers import tripwire
from pernix.fetchers.tripwire import Tripwire


class Tripped(Exception):
    pass


def _tripped(reason: str) -> Exception:
    return Tripped(reason)


async def test_tripping_cancels_the_block_and_raises_the_error_in_its_place() -> None:
    wire = Tripwire()
    entered = asyncio.Event()

    async def guarded() -> None:
        async with wire.guarding(_tripped):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(guarded())
    await entered.wait()
    wire.trip("crashed")

    with pytest.raises(Tripped, match="crashed") as raised:
        await task
    assert isinstance(raised.value.__cause__, asyncio.CancelledError)
    assert task.cancelling() == 0


async def test_a_wire_tripped_before_raises_at_once() -> None:
    wire = Tripwire()
    wire.trip("lost")
    wire.trip("again")  # the first reason stays

    with pytest.raises(Tripped, match="lost"):
        async with wire.guarding(_tripped):
            pytest.fail("the block runs")


async def test_a_cancellation_from_elsewhere_stays_a_cancellation() -> None:
    wire = Tripwire()
    entered = asyncio.Event()

    async def guarded() -> None:
        async with wire.guarding(_tripped):
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(guarded())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.reason is None


async def test_an_error_that_may_come_of_the_trip_waits_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tripwire, "GRACE", 5.0)
    wire = Tripwire()
    loop = asyncio.get_running_loop()

    loop.call_later(0.05, wire.trip, "crashed")
    with pytest.raises(Tripped, match="crashed") as raised:
        async with wire.guarding(_tripped, may_trip=lambda error: isinstance(error, ConnectionError)):
            raise ConnectionError("target closed")

    assert isinstance(raised.value.__cause__, ConnectionError)


async def test_an_error_that_may_come_of_the_trip_stays_when_it_is_not_tripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tripwire, "GRACE", 0.05)
    wire = Tripwire()

    with pytest.raises(ConnectionError, match="target closed"):
        async with wire.guarding(_tripped, may_trip=lambda error: isinstance(error, ConnectionError)):
            raise ConnectionError("target closed")


async def test_other_errors_go_through_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tripwire, "GRACE", 5.0)
    wire = Tripwire()
    started = time.monotonic()

    with pytest.raises(ValueError, match="parse"):
        async with wire.guarding(_tripped):
            raise ValueError("cannot parse")

    assert time.monotonic() - started < 1


async def test_a_block_done_before_its_cancellation_arrives_is_not_cancelled_later() -> None:
    wire = Tripwire()

    async def guarded() -> str:
        async with wire.guarding(_tripped):
            wire.trip("lost")  # the cancellation is requested, but the block awaits nothing more
        await asyncio.sleep(0)  # would raise the cancellation if it were still pending
        return "done"

    assert await asyncio.create_task(guarded()) == "done"


async def test_tripping_cancels_every_block_it_guards() -> None:
    wire = Tripwire()
    entered: list[asyncio.Event] = [asyncio.Event() for _ in range(3)]

    async def guarded(n: int) -> None:
        async with wire.guarding(_tripped):
            entered[n].set()
            await asyncio.sleep(30)

    tasks = [asyncio.create_task(guarded(n)) for n in range(3)]
    for event in entered:
        await event.wait()
    wire.trip("lost")

    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert [type(result) for result in results] == [Tripped] * 3
