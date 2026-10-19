"""The conformance suite every Frontier backend must pass.

Subclass `FrontierConformance` in a class whose name starts with `Test` and
implement `make_frontier()`; pytest then runs every test against the backend. The
tests are coroutines, so they need pytest-asyncio. A backend needing asynchronous
setup or teardown can override the `frontier` fixture.

Example:
    >>> class TestRedisFrontier(FrontierConformance):
    ...     def make_frontier(self, clock):
    ...         return RedisFrontier(url, clock=clock)
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from pernix.request import Request

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.frontiers import Frontier, Lease

__all__ = ["FakeClock", "FrontierConformance"]

TTL = 30.0
URL = "https://example.com/"


class FakeClock:
    """A clock that only moves when told to.

    Attributes:
        now: The current time, in seconds.
    """

    def __init__(self, now: float = 1_000.0) -> None:
        """Start the clock.

        Args:
            now: The initial time, in seconds.
        """
        self.now = now

    def __call__(self) -> float:
        """Return the current time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward.

        Args:
            seconds: How far.
        """
        self.now += seconds


def _page(n: int, **kwargs: Any) -> Request:
    return Request(f"{URL}{n}", **kwargs)


class FrontierConformance:
    """Tests of the Frontier contract; see the module description."""

    pytestmark = pytest.mark.asyncio

    def make_frontier(self, clock: Callable[[], float]) -> Frontier:
        """Create an empty Frontier of the backend under test.

        Args:
            clock: The clock the Frontier must use for Lease expiry and delays.

        Returns:
            The Frontier, using the default Fingerprint function and host slots, and
            telling rendered Requests apart with `pernix.fetchers.is_rendered()`.
        """
        raise NotImplementedError

    async def reopen(self, frontier: Frontier, clock: Callable[[], float]) -> Frontier:
        """Return a Frontier on the same storage as a closed one.

        Args:
            frontier: The closed Frontier.
            clock: The clock the Frontier must use.

        Returns:
            `frontier` itself by default; persistent backends return a new Frontier
            reading what the closed one stored.
        """
        return frontier

    @pytest.fixture
    def clock(self) -> FakeClock:
        """The clock of the Frontier under test."""
        return FakeClock()

    @pytest.fixture
    def frontier(self, clock: FakeClock) -> Frontier:
        """An empty Frontier of the backend under test."""
        return self.make_frontier(clock)

    async def _claim_one(self, frontier: Frontier, ttl: float = TTL) -> Lease:
        [lease] = await frontier.claim("worker-1", 1, ttl)
        return lease

    # Adding and deduplication

    async def test_add_stores_new_requests_and_rejects_duplicates(self, frontier: Frontier) -> None:
        assert await frontier.add([_page(1), _page(2)]) == [True, True]
        assert await frontier.add([_page(1), _page(3), _page(3)]) == [False, True, False]
        assert (await frontier.counts()).ready == 3

    async def test_the_default_fingerprint_decides_duplicates(self, frontier: Frontier) -> None:
        assert await frontier.add([Request(f"{URL}?b=2&a=1#top", cookies={"session": "x"})]) == [True]
        assert await frontier.add([Request("HTTPS://EXAMPLE.com:443/?a=1&b=2", cookies={"session": "y"})]) == [False]

    async def test_dont_filter_bypasses_deduplication(self, frontier: Frontier) -> None:
        added = await frontier.add([_page(1), _page(1, dont_filter=True), _page(1, dont_filter=True), _page(1)])

        assert added == [True, True, True, False]

    async def test_explicit_fingerprints_decide_deduplication(self, frontier: Frontier) -> None:
        added = await frontier.add(
            [_page(1, fingerprint="a"), _page(1, fingerprint="b"), _page(2, fingerprint="a"), _page(3, fingerprint="")]
        )

        assert added == [True, True, False, True]

    async def test_concurrent_adds_of_one_request_store_it_once(self, frontier: Frontier) -> None:
        results = await asyncio.gather(*(frontier.add([_page(1)]) for _ in range(20)))

        assert sorted(result for [result] in results) == [False] * 19 + [True]
        assert (await frontier.counts()).ready == 1

    # Claiming

    async def test_claims_go_by_priority_then_oldest_first(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2, priority=5), _page(3), _page(4, priority=5), _page(5, priority=-1)])

        leases = await frontier.claim("worker-1", 10, TTL)

        assert [lease.request.url for lease in leases] == [f"{URL}{n}" for n in (2, 4, 1, 3, 5)]

    async def test_claims_respect_the_limit_and_may_find_nothing(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2), _page(3)])

        assert len(await frontier.claim("worker-1", 2, TTL)) == 2
        assert len(await frontier.claim("worker-1", 2, TTL)) == 1
        assert await frontier.claim("worker-1", 2, TTL) == []

    async def test_leases_describe_the_claim(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([Request("https://Shop.Example.com/item?id=1")])

        lease = await self._claim_one(frontier)

        assert (lease.attempt, lease.expires_at, lease.slot) == (1, clock.now + TTL, "shop.example.com")
        assert lease.request.url == "https://Shop.Example.com/item?id=1"
        assert lease.id

    async def test_tokens_increase_with_every_claim(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2)])

        first = await self._claim_one(frontier)
        assert await frontier.nack(first)
        second = await self._claim_one(frontier)
        third = await self._claim_one(frontier)

        assert first.token < second.token < third.token
        assert third.id == first.id  # returned behind page 2

    async def test_requests_of_skipped_slots_wait(self, frontier: Frontier) -> None:
        await frontier.add(
            [Request("https://a.example/1"), Request("https://b.example/1"), Request("https://a.example/2")]
        )

        leases = await frontier.claim("worker-1", 10, TTL, skip_slots={"a.example"})
        later = await frontier.claim("worker-1", 10, TTL)

        assert [lease.request.url for lease in leases] == ["https://b.example/1"]
        assert [lease.request.url for lease in later] == ["https://a.example/1", "https://a.example/2"]

    async def test_claims_can_take_only_rendered_requests_or_only_the_others(self, frontier: Frontier) -> None:
        await frontier.add(
            [
                _page(1),
                _page(2, render=True),
                _page(3, fetcher="browser"),
                _page(4, render=True, fetcher="http"),  # the Fetcher it names fetches it
                _page(5),
            ]
        )

        rendered = await frontier.claim("worker-1", 10, TTL, rendered=True)
        others = await frontier.claim("worker-1", 10, TTL, rendered=False)

        assert [(lease.request.url, lease.rendered) for lease in rendered] == [(f"{URL}2", True), (f"{URL}3", True)]
        assert [(lease.request.url, lease.rendered) for lease in others] == [
            (f"{URL}1", False),
            (f"{URL}4", False),
            (f"{URL}5", False),
        ]

    async def test_claims_of_both_kinds_go_by_priority_then_oldest_first(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2, render=True), _page(3, priority=5), _page(4, render=True, priority=5)])

        leases = await frontier.claim("worker-1", 10, TTL)

        assert [lease.request.url for lease in leases] == [f"{URL}{n}" for n in (3, 4, 1, 2)]

    async def test_rendered_requests_stay_rendered_when_returned(self, frontier: Frontier) -> None:
        await frontier.add([_page(1, render=True), _page(2)])
        [lease] = await frontier.claim("worker-1", 1, TTL, rendered=True)

        assert await frontier.nack(lease)

        assert await frontier.claim("worker-1", 10, TTL, rendered=False) != []
        [again] = await frontier.claim("worker-1", 10, TTL, rendered=True)
        assert (again.id, again.rendered, again.attempt) == (lease.id, True, 2)

    async def test_a_request_returned_with_another_is_handed_out_as_that_one(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2)])
        [lease] = await frontier.claim("worker-1", 1, TTL, rendered=False)

        assert await frontier.nack(lease, budget="blocked", request=lease.request.replace(render=True))

        [again] = await frontier.claim("worker-1", 10, TTL, rendered=True)
        assert (again.id, again.rendered, again.request.render, dict(again.spent)) == (
            lease.id,
            True,
            True,
            {"blocked": 1},
        )
        assert await frontier.add([_page(1)]) == [False]  # its Fingerprint is the same

    async def test_skipped_slots_apply_to_either_kind(self, frontier: Frontier) -> None:
        await frontier.add([Request("https://a.example/1", render=True), Request("https://b.example/1", render=True)])

        leases = await frontier.claim("worker-1", 10, TTL, skip_slots={"a.example"}, rendered=True)

        assert [lease.request.url for lease in leases] == ["https://b.example/1"]
        assert [lease.request.url for lease in await frontier.claim("worker-1", 10, TTL)] == ["https://a.example/1"]

    @pytest.mark.parametrize(("limit", "ttl"), [(0, TTL), (1, 0.0), (1, -1.0)])
    async def test_invalid_claims_are_rejected(self, frontier: Frontier, limit: int, ttl: float) -> None:
        with pytest.raises(ValueError, match="must be"):
            await frontier.claim("worker-1", limit, ttl)

    # Settling

    async def test_acked_requests_are_done(self, frontier: Frontier) -> None:
        await frontier.add([_page(1)])
        lease = await self._claim_one(frontier)

        assert await frontier.ack(lease)
        counts = await frontier.counts()
        assert (counts.ready, counts.leased, counts.done, counts.unfinished) == (0, 0, 1, 0)
        assert await frontier.add([_page(1)]) == [False]

    async def test_stale_leases_cannot_write(self, frontier: Frontier) -> None:
        await frontier.add([_page(1)])
        lease = await self._claim_one(frontier)
        assert await frontier.ack(lease)

        assert not await frontier.ack(lease)
        assert not await frontier.nack(lease)
        assert not await frontier.dead(lease, "late")
        assert await frontier.renew(lease, TTL) is None
        assert (await frontier.counts()).done == 1

    async def test_nack_returns_a_request_behind_the_ready_ones(self, frontier: Frontier) -> None:
        await frontier.add([_page(1), _page(2)])
        first = await self._claim_one(frontier)

        assert await frontier.nack(first, reason="try later")
        leases = await frontier.claim("worker-1", 10, TTL)

        assert [(lease.request.url, lease.attempt) for lease in leases] == [(f"{URL}2", 1), (f"{URL}1", 2)]

    async def test_returns_under_a_retry_budget_are_counted_apart_from_the_attempts(self, frontier: Frontier) -> None:
        await frontier.add([_page(1)])
        first = await self._claim_one(frontier)
        assert (first.attempt, dict(first.spent)) == (1, {})

        assert await frontier.nack(first, budget="browser_lost")
        second = await self._claim_one(frontier)
        assert await frontier.nack(second, budget="browser_lost", count_attempt=True)  # the budget counts instead
        third = await self._claim_one(frontier)
        assert await frontier.nack(third, reason="HTTP 503")
        fourth = await self._claim_one(frontier)
        assert await frontier.nack(fourth, budget="session_switch")
        fifth = await self._claim_one(frontier)

        assert [lease.attempt for lease in (first, second, third, fourth, fifth)] == [1, 1, 1, 2, 2]
        assert dict(third.spent) == {"browser_lost": 2}
        assert dict(fifth.spent) == {"browser_lost": 2, "session_switch": 1}
        assert dict(first.spent) == {}  # a Lease handed out keeps what it was claimed with

    async def test_nack_with_a_delay_waits_until_it_passes(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([_page(1)])

        assert await frontier.nack(await self._claim_one(frontier), delay=10.0, count_attempt=False)
        assert (await frontier.counts()).delayed == 1
        clock.advance(9.9)
        assert await frontier.claim("worker-1", 1, TTL) == []
        clock.advance(0.1)

        lease = await self._claim_one(frontier)
        assert lease.attempt == 1
        assert (await frontier.counts()).delayed == 0

    async def test_requests_due_after_a_delay_queue_ahead_of_later_arrivals(
        self, frontier: Frontier, clock: FakeClock
    ) -> None:
        await frontier.add([_page(1)])
        assert await frontier.nack(await self._claim_one(frontier), delay=10.0)
        clock.advance(11.0)

        await frontier.add([_page(2)])
        leases = await frontier.claim("worker-1", 10, TTL)

        assert [lease.request.url for lease in leases] == [f"{URL}1", f"{URL}2"]

    async def test_dead_requests_are_kept_and_never_handed_out(self, frontier: Frontier) -> None:
        await frontier.add([_page(1)])
        lease = await self._claim_one(frontier)

        assert await frontier.dead(lease, "ValueError: cannot parse")

        counts = await frontier.counts()
        assert (counts.dead, counts.unfinished) == (1, 0)
        assert await frontier.claim("worker-1", 1, TTL) == []
        assert await frontier.add([_page(1)]) == [False]

    # Lease expiry

    async def test_renewed_leases_stay_valid(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([_page(1)])
        lease = await self._claim_one(frontier, ttl=10.0)
        clock.advance(8.0)

        renewed = await frontier.renew(lease, 10.0)
        clock.advance(8.0)

        assert renewed is not None
        assert (renewed.token, renewed.expires_at) == (lease.token, clock.now + 2.0)
        assert await frontier.claim("worker-2", 1, TTL) == []
        assert await frontier.ack(renewed)

    async def test_expired_leases_hand_the_request_out_again(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([_page(1)])
        stale = await self._claim_one(frontier, ttl=10.0)
        clock.advance(10.0)

        fresh = await self._claim_one(frontier)

        assert (fresh.id, fresh.attempt) == (stale.id, 2)
        assert fresh.token > stale.token
        assert not await frontier.ack(stale)
        assert await frontier.renew(stale, TTL) is None
        assert await frontier.ack(fresh)

    async def test_expired_leases_count_as_ready(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([_page(1)])
        await self._claim_one(frontier, ttl=10.0)
        assert (await frontier.counts()).leased == 1

        clock.advance(10.0)

        counts = await frontier.counts()
        assert (counts.ready, counts.leased) == (1, 0)

    # Waiting and closing

    async def test_wait_returns_once_a_request_is_added(self, frontier: Frontier) -> None:
        waiting = asyncio.create_task(frontier.wait())
        await asyncio.sleep(0)

        await frontier.add([_page(1)])

        async with asyncio.timeout(5):
            await waiting

    async def test_wait_returns_once_a_lease_claimed_while_waiting_expires(
        self, frontier: Frontier, clock: FakeClock
    ) -> None:
        await frontier.add([_page(1)])
        waiting = asyncio.create_task(frontier.wait())
        await asyncio.sleep(0)

        await self._claim_one(frontier, ttl=0.05)
        clock.advance(0.05)

        async with asyncio.timeout(5):
            await waiting
        assert (await frontier.counts()).ready == 1

    async def test_close_keeps_the_requests(self, frontier: Frontier, clock: FakeClock) -> None:
        await frontier.add([_page(1), _page(2)])
        await frontier.ack(await self._claim_one(frontier))
        before = await frontier.counts()

        await frontier.close()
        reopened = await self.reopen(frontier, clock)

        assert await reopened.counts() == before
        assert await reopened.add([_page(1), _page(2)]) == [False, False]
