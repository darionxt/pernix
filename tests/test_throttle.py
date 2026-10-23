from __future__ import annotations

import asyncio
import random
import time

import pytest

from pernix import Request, Response
from pernix.settings import DomainSettings
from pernix.testing.frontier import FakeClock
from pernix.throttle import SESSION_COOLDOWN_STATUSES, Throttle

A = "a.example"
B = "b.example"


class Pick(random.Random):
    """Jitter that always picks one end of the range."""

    def __init__(self, high: bool) -> None:
        super().__init__()
        self.high = high

    def uniform(self, a: float, b: float) -> float:
        return b if self.high else a


def _throttle(clock: FakeClock, **settings: object) -> Throttle:
    return Throttle(DomainSettings(**settings), clock=clock, rng=Pick(high=False))  # pyright: ignore[reportArgumentType]


def _response(status: int, retry_after: str | None = None) -> Response:
    headers = [] if retry_after is None else [("Retry-After", retry_after)]
    return Response(url=f"https://{A}/", status=status, headers=headers, body=b"", request=Request(f"https://{A}/"))


async def test_each_domain_has_its_own_concurrency_limit() -> None:
    throttle = _throttle(FakeClock(), concurrency=2)

    assert await throttle.acquire(A)
    assert await throttle.acquire(A)
    assert throttle.blocked() == {A}
    assert await throttle.acquire(B)

    third = asyncio.create_task(throttle.acquire(A))
    await asyncio.sleep(0.01)
    assert not third.done()
    throttle.release(A)
    async with asyncio.timeout(5):
        assert await third
    assert (throttle.stats.waits, throttle.stats.wait_seconds) == (1, 0.0)  # the fake clock stood still


async def test_a_shared_fetch_takes_no_place_but_keeps_the_pace() -> None:
    throttle = Throttle(DomainSettings(concurrency=1, delay=0.05, delay_jitter=0.0))
    assert await throttle.acquire(A)

    started = time.monotonic()
    async with asyncio.timeout(1):
        assert await throttle.acquire(A, shared=True)  # the only place is taken, and shared
    assert time.monotonic() - started >= 0.045  # after the delay all the same
    throttle.release(A)
    async with asyncio.timeout(1):
        assert await throttle.acquire(A)  # the shared fetch took no place

    throttle.observe(A, _response(429, "60"))
    assert not await throttle.acquire(A, shared=True)


async def test_delays_space_the_starts_of_one_domain() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, delay=2.0, delay_jitter=0.0)

    assert await throttle.acquire(A)
    throttle.release(A)
    assert throttle.blocked() == {A}
    clock.advance(1.9)
    assert throttle.blocked() == {A}
    clock.advance(0.1)
    assert throttle.blocked() == frozenset()
    assert await throttle.acquire(B)


@pytest.mark.parametrize(("high", "spacing"), [(False, 1.0), (True, 3.0)])
async def test_delays_are_jittered(high: bool, spacing: float) -> None:
    clock = FakeClock()
    throttle = Throttle(DomainSettings(delay=2.0, delay_jitter=0.5), clock=clock, rng=Pick(high))

    await throttle.acquire(A)
    throttle.release(A)
    clock.advance(spacing - 0.01)
    assert throttle.blocked() == {A}
    clock.advance(0.01)
    assert throttle.blocked() == frozenset()


async def test_the_rate_allows_a_burst_then_paces_with_gcra() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, rate=2.0, burst=3, concurrency=10)

    for _ in range(3):
        assert throttle.blocked() == frozenset()
        assert await throttle.acquire(A)
    assert throttle.blocked() == {A}
    clock.advance(0.49)
    assert throttle.blocked() == {A}
    clock.advance(0.01)
    assert throttle.blocked() == frozenset()
    assert await throttle.acquire(A)
    assert throttle.blocked() == {A}
    clock.advance(0.5)
    assert throttle.blocked() == frozenset()


async def test_a_retry_after_on_429_or_503_cools_the_domain_down() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, cooldown_max=60.0)

    throttle.observe(A, _response(429, "30"))

    assert throttle.cooldown_left(A) == 30.0
    assert throttle.blocked() == {A}
    assert not await throttle.acquire(A)
    assert await throttle.acquire(B)
    clock.advance(30.0)
    assert await throttle.acquire(A)
    assert (throttle.stats.cooldowns, throttle.stats.refused) == (1, 1)


async def test_spacing_a_domain_keeps_its_starts_apart_from_the_last_one() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, delay=1.0, delay_jitter=0.0)
    assert await throttle.acquire(A)
    throttle.release(A)
    clock.advance(0.5)

    throttle.space(A, 3.0)  # as a Crawl-delay learned once the fetch started

    assert throttle.blocked() == {A}
    clock.advance(2.49)
    assert throttle.blocked() == {A}
    assert await throttle.admit(A)  # held by a fetch, the slot keeps its spacing
    clock.advance(0.01)
    assert throttle.blocked() == frozenset()
    assert await throttle.pace(A)
    throttle.release(A)
    clock.advance(2.99)
    assert throttle.blocked() == {A}


async def test_a_slot_forgotten_once_idle_keeps_no_spacing() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, delay=1.0, delay_jitter=0.0)
    throttle.space(A, 3.0)
    throttle.space(B, 3.0)  # a slot nothing started in yet
    assert await throttle.acquire(A)
    throttle.release(A)
    clock.advance(3.0)
    assert throttle.blocked() == frozenset()  # forgotten, as fresh slots would behave the same

    assert await throttle.acquire(A)
    throttle.release(A)
    clock.advance(0.99)
    assert throttle.blocked() == {A}
    clock.advance(0.01)
    assert throttle.blocked() == frozenset()  # by the delay alone
    assert (throttle.spacing(A), throttle.spacing(B)) == (0.0, 0.0)
    throttle.space(A, 3.0)  # until spaced again
    assert await throttle.acquire(A)
    throttle.release(A)
    clock.advance(2.99)
    assert throttle.blocked() == {A}


@pytest.mark.parametrize(("spacing", "apart"), [(0.5, 2.0), (3.0, 3.0), (0.0, 2.0)])
async def test_the_larger_of_the_spacing_and_the_jittered_delay_applies(spacing: float, apart: float) -> None:
    clock = FakeClock()
    throttle = Throttle(DomainSettings(delay=1.0, delay_jitter=1.0), clock=clock, rng=Pick(high=True))
    throttle.space(A, 5.0)
    throttle.space(A, spacing)  # replaced, and 0 spaces by the delay alone again

    assert await throttle.acquire(A)
    throttle.release(A)
    clock.advance(apart - 0.01)
    assert throttle.blocked() == {A}
    clock.advance(0.01)
    assert throttle.blocked() == frozenset()


async def test_a_domain_cools_down_for_at_most_the_cap() -> None:
    clock = FakeClock()
    throttle = _throttle(clock, cooldown_max=60.0)

    assert throttle.cool_down(A, 90.0) == 60.0

    assert throttle.cooldown_left(A) == 60.0
    assert not await throttle.acquire(A)
    assert throttle.stats.cooldowns == 1


@pytest.mark.parametrize(
    ("status", "retry_after", "cooldown"),
    [(503, "600", 60.0), (503, None, 0.0), (429, "soon", 0.0), (404, "30", 0.0), (500, "30", 0.0)],
)
def test_only_429_and_503_with_retry_after_cool_down_up_to_the_cap(
    status: int, retry_after: str | None, cooldown: float
) -> None:
    throttle = _throttle(FakeClock(), cooldown_max=60.0)

    throttle.observe(A, _response(status, retry_after))

    assert throttle.cooldown_left(A) == cooldown


@pytest.mark.parametrize(("status", "cooldown"), [(429, 0.0), (503, 30.0)])
def test_under_a_session_only_503_cools_the_domain_down(status: int, cooldown: float) -> None:
    throttle = _throttle(FakeClock(), cooldown_max=60.0)

    throttle.observe(A, _response(status, "30"), statuses=SESSION_COOLDOWN_STATUSES)

    assert throttle.cooldown_left(A) == cooldown


async def test_wait_returns_when_a_fetch_ends() -> None:
    throttle = _throttle(FakeClock(), concurrency=1)
    await throttle.acquire(A)
    waiting = asyncio.create_task(throttle.wait())
    await asyncio.sleep(0)

    throttle.release(A)

    async with asyncio.timeout(5):
        await waiting


async def test_wait_returns_when_a_delay_passes() -> None:
    throttle = Throttle(DomainSettings(delay=0.05, delay_jitter=0.0))
    await throttle.acquire(A)
    throttle.release(A)

    async with asyncio.timeout(5):
        await throttle.wait()

    assert throttle.blocked() == frozenset()


async def test_waiting_for_a_slot_is_counted() -> None:
    throttle = Throttle(DomainSettings(delay=0.05, delay_jitter=0.0))
    await throttle.acquire(A)
    throttle.release(A)

    async with asyncio.timeout(5):
        assert await throttle.acquire(A)

    assert throttle.stats.waits == 1
    assert throttle.stats.wait_seconds >= 0.04


async def test_an_acquire_cancelled_while_it_waits_for_the_delay_takes_no_place() -> None:
    throttle = Throttle(DomainSettings(concurrency=1, delay=30.0, delay_jitter=0.0))
    assert await throttle.acquire(A)
    throttle.release(A)

    waiting = asyncio.create_task(throttle.acquire(A))  # it holds a place while waiting for the delay
    await asyncio.sleep(0.01)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    async with asyncio.timeout(1):
        assert await throttle.admit(A)  # the place is free again
