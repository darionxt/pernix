import asyncio

import pytest

from pernix import Cookie, Session
from pernix.errors import SessionError
from pernix.sessions import CookieRemoval, MemorySessionPool, Outcome, StateDelta
from pernix.stats import Stats

NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def _pool(**options: object) -> tuple[MemorySessionPool, Clock]:
    clock = Clock()
    return MemorySessionPool(clock=clock, **options), clock  # type: ignore[arg-type]


def _cookie(name: str, value: str, *, modified: float) -> Cookie:
    return Cookie(name=name, value=value, domain="example.com", created=NOW, modified=modified)


async def test_sessions_are_created_as_needed_up_to_the_size() -> None:
    stats = Stats()
    pool, _ = _pool(size=2, stats=stats)

    first = await pool.acquire(timeout=0)
    second = await pool.acquire(timeout=0)
    third = await pool.acquire(timeout=0)

    assert first is not None
    assert second is not None
    assert (first.session, second.session, third) == ("default-1", "default-2", None)
    assert (len(pool), stats.get("sessions.created")) == (2, 2)


async def test_an_eager_pool_creates_every_session_at_once() -> None:
    pool, _ = _pool(name="accounts", size=3, eager=True)

    assert len(pool) == 3
    assert pool.health("accounts-3") == "available"


async def test_a_free_session_is_leased_before_one_is_created_least_recently_leased_first() -> None:
    pool, clock = _pool(size=3)
    first = await pool.acquire(timeout=0)
    assert first is not None
    await pool.release(first, Outcome("ok"))
    clock.now += 1
    second = await pool.acquire(timeout=0)
    third = await pool.acquire(timeout=0)
    assert second is not None
    assert third is not None
    await pool.release(second, Outcome("ok"))
    await pool.release(third, Outcome("ok"))
    clock.now += 1

    again = await pool.acquire(timeout=0)

    assert (second.session, third.session) == ("default-1", "default-2")
    assert len(pool) == 2
    assert again is not None
    assert again.session == "default-1"


async def test_a_session_takes_its_max_concurrency_of_leases_at_once() -> None:
    pool, _ = _pool(size=1, max_concurrency=2)

    leases = [await pool.acquire(timeout=0) for _ in range(3)]

    assert [lease is not None for lease in leases] == [True, True, False]


async def test_a_waiting_acquire_gets_the_session_once_it_is_released() -> None:
    pool, _ = _pool(size=1)
    held = await pool.acquire()
    assert held is not None

    waiting = asyncio.create_task(pool.acquire(timeout=5))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await pool.release(held, Outcome("ok"))
    lease = await waiting

    assert lease is not None
    assert lease.session == held.session


async def test_a_named_session_is_created_under_its_id_while_there_is_room() -> None:
    pool, _ = _pool(size=2)

    alice = await pool.acquire(session="alice", timeout=0)
    bob = await pool.acquire(session="bob", timeout=0)

    assert alice is not None
    assert bob is not None
    assert (alice.session, bob.session) == ("alice", "bob")
    with pytest.raises(SessionError, match="holds 2 Sessions already, and none is 'carol'"):
        await pool.acquire(session="carol", timeout=0)
    with pytest.raises(SessionError, match="'carol'"):
        await pool.ready_in(session="carol")


async def test_a_busy_named_session_is_not_leased_twice() -> None:
    pool, _ = _pool()
    held = await pool.acquire(session="alice")

    assert held is not None
    assert await pool.acquire(session="alice", timeout=0) is None
    assert await pool.ready_in(session="alice") == 0.0  # busy, not cooling down


async def test_a_retired_session_is_never_leased_again() -> None:
    pool, _ = _pool(size=1)
    lease = await pool.acquire(session="alice")
    assert lease is not None
    await pool.release(lease, Outcome("ok"))
    pool.retire("alice")

    assert pool.health("alice") == "retired"
    with pytest.raises(SessionError, match="retired"):
        await pool.acquire(session="alice", timeout=0)
    with pytest.raises(SessionError, match="no Session that is not retired"):
        await pool.acquire(timeout=0)


async def test_tags_select_the_sessions_added_with_them() -> None:
    pool, _ = _pool(size=3)
    pool.add(Session("plain"))
    pool.add(Session("member", account="alice"), tags=["logged-in"], max_concurrency=1)

    lease = await pool.acquire(tags=["logged-in"], timeout=0)

    assert lease is not None
    assert lease.session == "member"
    assert await pool.acquire(tags=["logged-in"], timeout=0) is None  # busy, and none is created with tags
    with pytest.raises(SessionError, match="with the tags \\['admin'\\]"):
        await pool.acquire(tags=["admin"], timeout=0)
    with pytest.raises(ValueError, match="holds a Session 'plain' already"):
        pool.add(Session("plain"))


async def test_a_rate_limited_session_cools_down_until_its_time_while_others_are_leased() -> None:
    pool, clock = _pool(size=2)
    lease = await pool.acquire(session="alice")
    assert lease is not None

    await pool.release(lease, Outcome("rate_limited", until=NOW + 30))

    assert pool.health("alice") == "cooldown"
    assert await pool.acquire(session="alice", timeout=0) is None
    assert await pool.ready_in(session="alice") == 30.0
    other = await pool.acquire(timeout=0)
    assert other is not None
    assert other.session != "alice"
    clock.now += 30
    assert pool.health("alice") == "available"
    assert await pool.acquire(session="alice", timeout=0) is not None


async def test_other_outcomes_leave_a_session_available() -> None:
    pool, _ = _pool(size=1)
    for kind in ("ok", "blocked", "auth_expired", "proxy_failed", "error"):
        lease = await pool.acquire(timeout=0)
        assert lease is not None
        await pool.release(lease, Outcome(kind))  # type: ignore[arg-type]

    assert pool.health("default-1") == "available"


async def test_a_stale_lease_is_rejected_once_expired_or_released() -> None:
    pool, clock = _pool(lease_ttl=10)
    expired = await pool.acquire(session="alice")
    assert expired is not None
    clock.now += 10

    assert not await pool.release(expired, Outcome("rate_limited", until=NOW + 99))
    assert not await pool.save_state(expired, StateDelta(cookies=[_cookie("a", "1", modified=NOW)]))
    assert not await pool.renew(expired)
    with pytest.raises(SessionError, match="expired or was released"):
        await pool.load_state(expired)
    assert pool.health("alice") == "available"  # the stale release changed nothing

    released = await pool.acquire(session="alice")
    assert released is not None
    assert await pool.release(released, Outcome("ok"))
    assert not await pool.release(released, Outcome("ok"))
    current = await pool.acquire(session="alice", timeout=0)
    assert current is not None
    assert (await pool.load_state(current)).cookies == []


async def test_renewing_keeps_a_lease_past_its_ttl() -> None:
    pool, clock = _pool(lease_ttl=10)
    lease = await pool.acquire()
    assert lease is not None

    clock.now += 8
    assert await pool.renew(lease)
    clock.now += 8

    assert await pool.release(lease, Outcome("ok"))


async def test_an_expired_lease_frees_its_session() -> None:
    pool, clock = _pool(size=1, lease_ttl=10)
    assert await pool.acquire() is not None
    assert await pool.acquire(timeout=0) is None

    clock.now += 10

    assert await pool.acquire(timeout=0) is not None


async def test_states_merge_cookie_by_cookie_by_modification_time() -> None:
    pool, _ = _pool(max_concurrency=2)
    pool.add(Session("s", cookies=[_cookie("a", "old", modified=NOW), _cookie("b", "old", modified=NOW)]))
    first, second = await pool.acquire(session="s"), await pool.acquire(session="s")
    assert first is not None
    assert second is not None

    assert await pool.save_state(first, StateDelta(cookies=[_cookie("a", "first", modified=NOW + 2)]))
    assert await pool.save_state(
        second,
        StateDelta(cookies=[_cookie("a", "second", modified=NOW + 1), _cookie("c", "second", modified=NOW + 1)]),
    )  # its a is older than the one saved first

    state = await pool.load_state(first)
    assert [(cookie.name, cookie.value) for cookie in state.cookies] == [("a", "first"), ("b", "old"), ("c", "second")]


async def test_a_removed_cookie_stays_removed_against_older_changes() -> None:
    pool, _ = _pool(max_concurrency=2)
    pool.add(Session("s", cookies=[_cookie("a", "1", modified=NOW)]))
    lease = await pool.acquire(session="s")
    assert lease is not None
    removal = CookieRemoval(name="a", domain="example.com", host_only=True, path="/", removed=NOW + 5)

    await pool.save_state(lease, StateDelta(removed=[removal]))
    await pool.save_state(lease, StateDelta(cookies=[_cookie("a", "stale", modified=NOW + 3)]))
    assert (await pool.load_state(lease)).cookies == []
    await pool.save_state(lease, StateDelta(cookies=[_cookie("a", "fresh", modified=NOW + 6)]))

    assert [cookie.value for cookie in (await pool.load_state(lease)).cookies] == ["fresh"]


async def test_local_storage_is_replaced_per_origin() -> None:
    pool, _ = _pool()
    pool.add(Session("s", local_storage={"https://a.example": {"k": "1"}, "https://b.example": {"k": "1"}}))
    lease = await pool.acquire(session="s")
    assert lease is not None

    await pool.save_state(lease, StateDelta(local_storage={"https://a.example": {"k": "2"}, "https://b.example": {}}))

    assert (await pool.load_state(lease)).local_storage == {"https://a.example": {"k": "2"}}


async def test_the_state_loaded_is_a_copy() -> None:
    pool, _ = _pool()
    pool.add(Session("s", local_storage={"https://a.example": {"k": "1"}}))
    lease = await pool.acquire(session="s")
    assert lease is not None

    (await pool.load_state(lease)).local_storage["https://a.example"]["k"] = "changed"

    assert (await pool.load_state(lease)).local_storage == {"https://a.example": {"k": "1"}}
