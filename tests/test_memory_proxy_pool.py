"""The in-process Proxy Pool: turns, candidates and bindings, cooldowns, Retirements, and what the sources list."""

from __future__ import annotations

import math

from pernix.proxies import MemoryProxyPool, Proxy, ProxyCandidate, ProxyCounts
from pernix.stats import Stats

A, B, C = "http://a.example:8080", "http://b.example:8080", "http://c.example:8080"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


async def _pool(
    *proxies: str, clock: Clock | None = None, stats: Stats | None = None, **options: float
) -> MemoryProxyPool:
    pool = MemoryProxyPool(clock=clock or Clock(), stats=stats, **options)  # pyright: ignore[reportArgumentType]
    await pool.replace([Proxy(proxy) for proxy in proxies])
    return pool


async def _sessions(pool: MemoryProxyPool) -> dict[str, frozenset[str]]:
    return {candidate.url: candidate.sessions for candidate in await pool.candidates()}


async def test_fetches_take_the_proxies_in_turn() -> None:
    pool = await _pool(A, B, C)

    assert [await pool.take() for _ in range(5)] == [A, B, C, A, B]


async def test_the_candidates_are_the_proxies_listed_least_recently_used_first() -> None:
    clock = Clock()
    pool = await _pool(A, B, C, clock=clock, max_failures=1)
    await pool.replace([Proxy(A, tags=frozenset({"us"}), expires=NOW + 60), Proxy(B), Proxy(C)])
    await pool.bind("one", B)
    await pool.take()  # A, unused until now
    await pool.report(C, failed=True, since=clock.now)

    assert await pool.candidates() == (
        ProxyCandidate(C, health="retired", failures=1),
        ProxyCandidate(B, sessions=frozenset({"one"})),
        ProxyCandidate(A, tags=frozenset({"us"}), expires=NOW + 60),
    )


async def test_a_session_is_bound_to_one_proxy_at_a_time_which_its_leases_share() -> None:
    stats = Stats()
    pool = await _pool(A, B, stats=stats)

    bound = [await pool.bind("one", A), await pool.bind("one", A), await pool.bind("one", B)]

    assert bound == [True, True, True]
    assert await _sessions(pool) == {A: frozenset(), B: frozenset({"one"})}
    assert stats.get("proxies.bindings") == 2  # keeping its proxy is no new binding


async def test_a_session_is_bound_only_to_a_proxy_available_with_room_for_it() -> None:
    clock = Clock()
    pool = await _pool(A, B, C, clock=clock, max_sessions=1)
    await pool.bind("one", A)
    await pool.report(B, failed=True, since=clock.now)
    await pool.replace([Proxy(A), Proxy(B)])

    refused = [await pool.bind("two", A), await pool.bind("two", B), await pool.bind("two", C)]

    assert refused == [False, False, False]  # full, cooling down, and no longer listed
    assert await pool.bind("one", A)  # its own has room for it
    assert await _sessions(pool) == {A: frozenset({"one"}), B: frozenset()}


async def test_a_session_bound_to_another_counts_toward_the_proxy_its_leases_still_go_through() -> None:
    pool = await _pool(A, B, max_sessions=1)
    await pool.bind("one", A)
    await pool.bind("one", B)  # an overlapping Lease rebinds it, while the first still goes through A

    refused = await pool.bind("two", A)
    leaving = [candidate.leaving for candidate in await pool.candidates()]
    await pool.release("one", A)  # the first Lease ended

    assert not refused
    assert leaving == [frozenset({"one"}), frozenset()]
    assert await pool.bind("two", A)


async def test_a_session_unbound_no_longer_counts_toward_its_proxy_once_its_leases_end() -> None:
    pool = await _pool(A, max_sessions=1)
    await pool.bind("one", A)
    await pool.release("one", A)
    still_bound = await pool.bind("two", A)

    await pool.unbind("one")
    await pool.unbind("never bound")

    assert not still_bound
    assert await pool.bind("two", A)
    assert await _sessions(pool) == {A: frozenset({"two"})}


async def test_a_failure_cools_a_proxy_down_longer_with_each_in_a_row_then_retires_it() -> None:
    clock = Clock()
    stats = Stats()
    pool = await _pool(A, clock=clock, stats=stats, cooldown=10, cooldown_max=25, max_failures=4)
    waits: list[float] = []

    for _ in range(3):
        await pool.report(A, failed=True, since=clock.now)
        waits.append(await pool.ready_in())
        assert pool.health(A) == "cooldown"
        assert await pool.take() is None
        clock.now += waits[-1]
    await pool.report(A, failed=True, since=clock.now)

    assert waits == [10, 20, 25]
    assert pool.health(A) == "retired"
    assert await pool.ready_in() == math.inf
    assert (stats.get("proxies.failures"), stats.get("proxies.cooldowns"), stats.get("proxies.retirements")) == (
        4,
        3,
        1,
    )


async def test_a_fetch_that_got_through_ends_the_row() -> None:
    clock = Clock()
    pool = await _pool(A, clock=clock, cooldown=10, max_failures=2)

    await pool.report(A, failed=True, since=clock.now)
    clock.now += 10
    await pool.report(A, failed=False, since=clock.now)
    await pool.report(A, failed=True, since=clock.now)

    assert pool.health(A) == "cooldown"
    assert await pool.ready_in() == 10  # the first of a new row


async def test_the_fetches_of_one_outage_count_once() -> None:
    clock = Clock()
    stats = Stats()
    pool = await _pool(A, clock=clock, stats=stats, cooldown=10, max_failures=2)
    started = clock.now
    clock.now += 1

    for _ in range(5):  # concurrent fetches through it, all failing as it went down
        await pool.report(A, failed=True, since=started)
    await pool.report(A, failed=False, since=started)  # one that started before tells nothing either

    assert pool.health(A) == "cooldown"
    assert await pool.ready_in() == 10
    assert (stats.get("proxies.failures"), stats.get("proxies.cooldowns")) == (5, 1)


async def test_a_proxy_no_longer_listed_is_not_used_but_keeps_its_health() -> None:
    clock = Clock()
    pool = await _pool(A, B, clock=clock, max_failures=1)
    await pool.report(B, failed=True, since=clock.now)

    await pool.replace([Proxy(A)])
    assert [await pool.take() for _ in range(2)] == [A, A]
    assert not await pool.bind("one", B)
    assert pool.health(B) is None

    await pool.replace([Proxy(B), Proxy(C)])
    assert pool.health(B) == "retired"
    assert await pool.take() == C


async def test_a_proxy_listed_again_takes_its_new_tags_and_the_first_given_twice_is_kept() -> None:
    pool = await _pool()

    await pool.replace([Proxy(A, tags=frozenset({"old"})), Proxy(A, tags=frozenset({"twice"}))])
    first = await pool.candidates()
    await pool.replace([Proxy(A, tags=frozenset({"new"}), expires=NOW)])

    assert [candidate.tags for candidate in first] == [frozenset({"old"})]
    assert [(candidate.tags, candidate.expires) for candidate in await pool.candidates()] == [(frozenset({"new"}), NOW)]


async def test_ready_in_tells_how_long_until_one_is_available_or_that_none_ever_will_be() -> None:
    clock = Clock()
    pool = await _pool(A, B, clock=clock, cooldown=10, max_failures=2)
    assert await pool.ready_in() == 0

    await pool.report(A, failed=True, since=clock.now)
    clock.now += 4
    await pool.report(B, failed=True, since=clock.now)
    assert await pool.ready_in() == 6

    clock.now += 20
    for proxy in (A, B):
        await pool.report(proxy, failed=True, since=clock.now)
    assert await pool.ready_in() == math.inf
    assert await (await _pool()).ready_in() == math.inf


async def test_counts_tell_the_state_of_the_proxies_listed() -> None:
    clock = Clock()
    pool = await _pool(A, B, C, clock=clock, max_failures=2)
    await pool.report(A, failed=True, since=clock.now)
    await pool.report(B, failed=True, since=clock.now)
    clock.now += 1
    await pool.report(B, failed=True, since=clock.now)

    assert pool.counts() == ProxyCounts(available=1, cooldown=1, retired=1)
    assert len(pool) == 3


async def test_proxies_the_pool_does_not_hold_are_ignored() -> None:
    pool = await _pool(A)

    await pool.report("http://other.example:1", failed=True, since=NOW)

    assert pool.counts() == ProxyCounts(available=1, cooldown=0, retired=0)
