from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Collection
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.fetchers import is_rendered
from pernix.frontiers import MemoryFrontier, host_slot
from pernix.testing.frontier import FakeClock, FrontierConformance

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pernix.frontiers import Frontier, Lease

URL = "https://example.com/"


class CountedSlots(Collection[str]):
    """Slots to skip, counting the times a claim asks whether a slot is one of them."""

    def __init__(self, *slots: str) -> None:
        self._slots = frozenset(slots)
        self.checks = 0

    def __contains__(self, slot: object) -> bool:
        self.checks += 1
        return slot in self._slots

    def __iter__(self) -> Iterator[str]:
        return iter(self._slots)

    def __len__(self) -> int:
        return len(self._slots)


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


async def test_a_claim_skips_a_slot_at_once_however_many_requests_wait_in_it() -> None:
    async def checks(waiting: int) -> int:
        frontier = MemoryFrontier()
        await frontier.add([Request(f"https://a.example/{n}") for n in range(waiting)])
        await frontier.add(
            [Request("https://a.example/rendered", render=True), Request("https://b.example/", priority=-1)]
        )
        skip = CountedSlots("a.example")

        leases = await frontier.claim("worker-1", 10, 30.0, skip)

        assert [lease.request.url for lease in leases] == ["https://b.example/"]
        return skip.checks

    assert await checks(1) == await checks(1_000)


async def test_a_claim_skips_a_slot_at_once_however_often_its_first_request_was_overtaken() -> None:
    async def checks(overtaken: int) -> int:
        frontier = MemoryFrontier()
        await frontier.add([Request("https://a.example/first")])
        for n in range(overtaken):
            await frontier.add([Request(f"https://a.example/{n}", priority=1)])
            [lease] = await frontier.claim("worker-1", 1, 30.0)
            assert lease.request.url == f"https://a.example/{n}"
        await frontier.add([Request("https://b.example/", priority=-1)])
        skip = CountedSlots("a.example")

        leases = await frontier.claim("worker-1", 10, 30.0, skip)

        assert [lease.request.url for lease in leases] == ["https://b.example/"]
        return skip.checks

    assert await checks(1) == await checks(100)


async def test_claims_go_by_priority_then_oldest_first_across_slots_skipped_or_not() -> None:
    rng = random.Random(115)  # noqa: S311  # seeded, the same steps every run
    frontier = MemoryFrontier(clock=FakeClock())
    hosts = ["a.example", "b.example", "c.example", "d.example"]
    ready: dict[str, tuple[int, int]] = {}  # (-priority, the order it became ready in), by URL
    became_ready = itertools.count()
    requests: dict[str, Request] = {}
    leases: list[Lease] = []
    for step in range(2000):
        action = rng.choice(["add", "claim", "claim", "return"])
        if action == "add":
            added = [
                Request(
                    f"https://{rng.choice(hosts)}/{step}/{n}",
                    priority=rng.choice([-1, 0, 0, 1, 5]),
                    render=rng.random() < 0.3,
                )
                for n in range(rng.randint(1, 4))
            ]
            assert await frontier.add(added) == [True] * len(added)
            for request in added:
                requests[request.url] = request
                ready[request.url] = (-request.priority, next(became_ready))
        elif action == "claim":
            limit, rendered = rng.randint(1, 4), rng.choice([None, True, False])
            skip = set(rng.sample(hosts, rng.randint(0, 3)))
            claimable = [
                url
                for url in ready
                if host_slot(requests[url]) not in skip and rendered in (None, is_rendered(requests[url]))
            ]
            claimed = await frontier.claim("worker-1", limit, 30.0, skip, rendered=rendered)
            assert [lease.request.url for lease in claimed] == sorted(claimable, key=ready.__getitem__)[:limit]
            for lease in claimed:
                del ready[lease.request.url]
            leases.extend(claimed)
        elif action == "return" and leases:
            lease = leases.pop(rng.randrange(len(leases)))
            request = lease.request
            if rng.random() < 0.3:
                request = request.replace(priority=rng.choice([-1, 2, 7]), render=not request.render)
            assert await frontier.nack(lease, request=request)
            requests[request.url] = request
            ready[request.url] = (-request.priority, next(became_ready))
