from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from pernix.fetchers.curl_session_pool import CurlSessionPool

if TYPE_CHECKING:
    from pernix.fetchers.curl_session_pool import CurlSession


class FakeSession:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.closed = False

    async def close(self) -> None:
        await asyncio.sleep(0)  # as closing a real session does, other tasks run meanwhile
        self.closed = True


class FakePool(CurlSessionPool):
    def __init__(self, *, size: int = 2, idle: float = 60) -> None:
        super().__init__(size=size, idle=idle, max_clients=4)
        self.created: list[FakeSession] = []

    def _new_session(self, profile: str) -> CurlSession:
        session = FakeSession(profile)
        self.created.append(session)
        return cast("CurlSession", session)


async def _use(pool: CurlSessionPool, profile: str) -> FakeSession:
    async with pool.session(profile) as session:
        return cast("FakeSession", session)


async def test_each_profile_has_its_own_session_reused_across_uses() -> None:
    pool = FakePool()

    first, other, again = await _use(pool, "chrome150"), await _use(pool, "chrome136"), await _use(pool, "chrome150")

    assert first is again
    assert other is not first
    assert [session.profile for session in pool.created] == ["chrome150", "chrome136"]


async def test_the_least_recently_used_session_goes_first_once_the_pool_is_full() -> None:
    pool = FakePool(size=2)
    a, b = await _use(pool, "a"), await _use(pool, "b")
    await _use(pool, "a")

    c = await _use(pool, "c")

    assert (a.closed, b.closed, c.closed) == (False, True, False)


async def test_sessions_in_use_are_never_closed() -> None:
    pool = FakePool(size=1)

    async with pool.session("a") as a, pool.session("b") as b:
        await _use(pool, "c")
        assert not cast("FakeSession", a).closed
        assert not cast("FakeSession", b).closed


async def test_idle_sessions_are_closed_when_the_pool_is_next_used() -> None:
    pool = FakePool(size=8, idle=0.01)
    idle = await _use(pool, "a")
    await asyncio.sleep(0.02)

    await _use(pool, "b")

    assert idle.closed


async def test_closing_the_pool_closes_every_session() -> None:
    pool = FakePool()
    sessions = [await _use(pool, "a"), await _use(pool, "b")]

    await pool.close()

    assert all(session.closed for session in sessions)


async def test_an_idle_session_is_replaced_rather_than_reused() -> None:
    pool = FakePool(size=8, idle=0.01)
    old = await _use(pool, "a")
    await asyncio.sleep(0.02)

    new = await _use(pool, "a")

    assert new is not old
    assert old.closed


async def test_sessions_being_closed_are_never_handed_out() -> None:
    pool = FakePool(size=8, idle=0.01)
    a, b = await _use(pool, "a"), await _use(pool, "b")
    await asyncio.sleep(0.02)  # both are idle now

    async def take(profile: str) -> bool:
        async with pool.session(profile) as session:
            await asyncio.sleep(0.01)
            return cast("FakeSession", session).closed

    closed_while_in_use = await asyncio.gather(take("c"), take("b"), take("a"))

    assert closed_while_in_use == [False, False, False]
    assert (a.closed, b.closed) == (True, True)
