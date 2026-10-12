from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.frontiers import MemoryFrontier, host_slot
from pernix.testing.frontier import FakeClock, FrontierConformance

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.frontiers import Frontier

URL = "https://example.com/"


class TestMemoryFrontier(FrontierConformance):
    def make_frontier(self, clock: Callable[[], float]) -> Frontier:
        return MemoryFrontier(clock=clock)


async def test_the_fingerprint_and_slot_functions_are_replaceable() -> None:
    frontier = MemoryFrontier(fingerprint=lambda request: request.url.lower(), slot_of=lambda request: "everything")

    assert await frontier.add([Request(URL + "A"), Request(URL + "a"), Request(URL + "#b")]) == [True, False, True]
    assert {lease.slot for lease in await frontier.claim("worker-1", 10, 30.0)} == {"everything"}


def test_hosts_are_the_default_slots() -> None:
    assert host_slot(Request("https://User@Shop.Example.COM:8443/a")) == "shop.example.com"


async def test_wait_returns_when_a_delay_passes() -> None:
    frontier = MemoryFrontier()
    await frontier.add([Request(URL)])
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    await frontier.nack(lease, delay=0.05)

    async with asyncio.timeout(5):
        await frontier.wait()

    assert (await frontier.counts()).ready == 1


async def test_wait_returns_when_a_lease_expires() -> None:
    clock = FakeClock()
    frontier = MemoryFrontier(clock=clock)
    await frontier.add([Request(URL)])
    await frontier.claim("worker-1", 1, 0.05)
    waiting = asyncio.create_task(frontier.wait())
    clock.advance(0.05)

    async with asyncio.timeout(5):
        await waiting

    assert (await frontier.counts()).ready == 1


async def test_a_failing_fingerprint_adds_nothing_of_the_batch() -> None:
    def fingerprint(request: Request) -> str:
        if request.url.endswith("bad"):
            raise ValueError("cannot fingerprint")
        return request.url

    frontier = MemoryFrontier(fingerprint=fingerprint)

    with pytest.raises(ValueError, match="cannot fingerprint"):
        await frontier.add([Request(URL + "good"), Request(URL + "bad")])

    assert (await frontier.counts()).ready == 0
    assert await frontier.add([Request(URL + "good")]) == [True]


async def test_a_failing_slot_function_adds_nothing_of_the_batch() -> None:
    def slot_of(request: Request) -> str:
        if request.url.endswith("bad"):
            raise ValueError("no slot")
        return "all"

    frontier = MemoryFrontier(slot_of=slot_of)

    with pytest.raises(ValueError, match="no slot"):
        await frontier.add([Request(URL + "good"), Request(URL + "bad")])

    assert (await frontier.counts()).ready == 0
    assert await frontier.add([Request(URL + "good")]) == [True]
