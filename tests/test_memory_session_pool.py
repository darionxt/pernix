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


@pytest.mark.parametrize("kind", ["rate_limited", "proxy_failed", "proxy_cooldown", "error"])
async def test_any_outcome_with_an_until_cools_its_session_down(kind: str) -> None:
    pool, clock = _pool(size=1)
    lease = await pool.acquire(session="alice")
    assert lease is not None

    await pool.release(lease, Outcome(kind, until=NOW + 60))  # type: ignore[arg-type]

    assert pool.health("alice") == "cooldown"
    assert await pool.ready_in(session="alice") == 60.0
    clock.now += 60
    assert pool.health("alice") == "available"


async def test_a_session_whose_proxy_is_retired_is_retired_with_it_each_retirement_counted_once() -> None:
    stats = Stats()
    pool, _ = _pool(size=3, logs_in=True, login_attempts=1, stats=stats)
    pool.add(Session("carol"))
    for session, outcome in (("alice", "proxy_retired"), ("bob", "login_failed")):
        lease = await pool.acquire(session=session)
        assert lease is not None
        await pool.release(lease, Outcome(outcome))  # type: ignore[arg-type]
    pool.retire("carol")
    pool.retire("alice")

    assert [pool.health(session) for session in ("alice", "bob", "carol")] == ["retired"] * 3
    assert stats.get("sessions.retirements") == 3


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


async def test_a_session_that_logs_in_is_leased_to_one_fetch_until_it_logged_in() -> None:
    pool, _ = _pool(max_concurrency=2, logs_in=True)

    first = await pool.acquire(session="s", timeout=0)
    assert first is not None
    assert first.must_log_in
    assert await pool.acquire(session="s", timeout=0) is None  # busy while logging in
    assert await pool.logged_in(first)
    second = await pool.acquire(session="s", timeout=0)

    assert second is not None
    assert not second.must_log_in
    assert not await pool.logged_in(second)  # it was not to log in


async def test_a_waiting_acquire_gets_the_session_once_it_logged_in() -> None:
    pool, _ = _pool(max_concurrency=2, logs_in=True)
    first = await pool.acquire(session="s")
    assert first is not None

    waiting = asyncio.create_task(pool.acquire(session="s", timeout=5))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    await pool.logged_in(first)
    lease = await waiting

    assert lease is not None
    assert not lease.must_log_in


@pytest.mark.parametrize("ending", ["released", "expired"])
async def test_a_login_given_up_lets_another_lease_log_in(ending: str) -> None:
    pool, clock = _pool(logs_in=True, lease_ttl=10)
    first = await pool.acquire(session="s")
    assert first is not None

    if ending == "released":
        await pool.release(first, Outcome("ok"))
    else:
        clock.now += 10
    again = await pool.acquire(session="s", timeout=0)

    assert again is not None
    assert again.must_log_in


async def test_auth_expired_has_the_session_log_in_again_unless_it_logged_in_since() -> None:
    pool, _ = _pool(max_concurrency=2, logs_in=True)
    first = await pool.acquire(session="s")
    assert first is not None
    await pool.logged_in(first)
    older = await pool.acquire(session="s")  # leased before the login expired
    assert older is not None

    await pool.release(first, Outcome("auth_expired"))
    relogin = await pool.acquire(session="s", timeout=0)
    assert relogin is not None
    assert relogin.must_log_in
    await pool.logged_in(relogin)
    await pool.release(older, Outcome("auth_expired"))  # what it saw was the login before
    after = await pool.acquire(session="s", timeout=0)

    assert after is not None
    assert not after.must_log_in


async def test_failed_logins_cool_the_session_down_then_retire_it() -> None:
    pool, clock = _pool(logs_in=True, login_attempts=2)
    lease = await pool.acquire(session="s")
    assert lease is not None

    await pool.release(lease, Outcome("login_failed", until=NOW + 10))
    assert pool.health("s") == "cooldown"
    clock.now += 10
    again = await pool.acquire(session="s", timeout=0)
    assert again is not None
    assert again.must_log_in
    await pool.release(again, Outcome("login_failed", until=NOW + 20))

    assert pool.health("s") == "retired"


async def test_a_login_that_worked_starts_the_count_of_failures_again() -> None:
    pool, _ = _pool(logs_in=True, login_attempts=2)
    for outcome in ("login_failed", "ok", "login_failed"):
        lease = await pool.acquire(session="s")
        assert lease is not None
        if outcome == "ok":
            await pool.logged_in(lease)
        await pool.release(lease, Outcome(outcome))  # type: ignore[arg-type]

    assert pool.health("s") == "available"


async def test_without_logins_no_lease_logs_its_session_in() -> None:
    pool, _ = _pool()

    lease = await pool.acquire(session="s")

    assert lease is not None
    assert not lease.must_log_in


async def test_a_failed_login_of_a_login_replaced_since_changes_nothing() -> None:
    pool, _ = _pool(max_concurrency=3, logs_in=True, login_attempts=1)
    first = await pool.acquire(session="s")
    assert first is not None
    await pool.logged_in(first)  # its fetch is still in flight
    other = await pool.acquire(session="s")
    assert other is not None
    await pool.release(other, Outcome("auth_expired"))
    relogin = await pool.acquire(session="s")
    assert relogin is not None
    await pool.logged_in(relogin)

    await pool.release(first, Outcome("login_failed", until=NOW + 30))  # logged out, under the replaced login
    after = await pool.acquire(session="s", timeout=0)

    assert pool.health("s") == "available"
    assert after is not None
    assert not after.must_log_in
