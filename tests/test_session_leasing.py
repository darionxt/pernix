"""Leasing Sessions for fetches, against a fake Fetcher that sees the Session it is given."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Account, Cookie, Request, Session
from pernix.errors import ConnectError, LoginExpired, ProxyError, SessionError
from pernix.fetchers import FetcherDispatch
from pernix.fetchers.fetcher_dispatch import sending_as
from pernix.proxies import MemoryProxyPool
from pernix.proxies.proxy_sources import StaticProxies
from pernix.proxies.run_proxies import NoProxyYet, RunProxies
from pernix.sessions import MemorySessionPool, Outcome, StateDelta
from pernix.sessions.session_leasing import NoSessionYet, SessionLeasing, sending_session
from pernix.sessions.session_login import SessionLogin
from pernix.settings import SessionSettings
from pernix.stats import Stats
from pernix.testing import make_response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from pernix import Response

SITE = "https://site.example"
NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Site:
    """A Fetcher answering every Request with a status and headers, keeping the cookies they set in its Session."""

    schemes: ClassVar[frozenset[str]] = frozenset({"https"})

    def __init__(self, status: int = 200, headers: list[tuple[str, str]] | None = None) -> None:
        self.status = status
        self.headers = headers or []
        self.error: Exception | None = None
        self.sent: list[tuple[str | None, str | None]] = []

    async def fetch(self, request: Request, *, session: Session | None = None) -> Response:
        self.sent.append((request.session, None if session is None else session.id))
        if self.error is not None:
            raise self.error
        if session is not None:
            session.cookies.receive(request.url, [value for name, value in self.headers if name == "Set-Cookie"])
        return make_response(request.url, status=self.status, headers=self.headers, request=request).replace(
            session=None if session is None else session.id
        )


def _leasing(
    *,
    clock: Clock | None = None,
    login: SessionLogin | None = None,
    proxies: RunProxies | None = None,
    **settings: Any,
) -> tuple[SessionLeasing, MemorySessionPool, Stats, Clock]:
    clock, stats = clock or Clock(), Stats()
    config = SessionSettings(enabled=True, **settings)
    pool = MemorySessionPool(
        size=config.size,
        max_concurrency=config.max_concurrency,
        lease_ttl=config.lease_ttl,
        logs_in=login is not None,
        login_attempts=config.login_attempts,
        stats=stats,
        clock=clock,
    )
    leasing = SessionLeasing(pool, config, stats=stats, login=login, proxies=proxies, clock=clock)
    return leasing, pool, stats, clock


async def _send(leasing: SessionLeasing, dispatch: FetcherDispatch, request: Request) -> Response:
    async with leasing.sending(request, wait=0) as sending:
        response = await dispatch(sending.request)
        sending.received(response)
    return response


def _login(
    log_in: Callable[[Session, Account | None], Awaitable[None]],
    is_logged_out: Callable[[Response], bool] = lambda response: response.status == 401,
    accounts: tuple[Account, ...] = (),
) -> SessionLogin:
    return SessionLogin(log_in, is_logged_out, accounts=accounts)


async def test_a_request_is_sent_under_the_session_leased_for_it() -> None:
    leasing, _, stats, _ = _leasing()
    site = Site()

    response = await _send(leasing, FetcherDispatch({"site": site}), Request(f"{SITE}/"))

    assert site.sent == [("default-1", "default-1")]
    assert (response.session, response.request.session) == ("default-1", "default-1")
    assert (stats.get("sessions.leased"), stats.get("sessions.outcomes.ok")) == (1, 1)


async def test_a_named_session_is_the_one_leased() -> None:
    leasing, _, _, _ = _leasing()
    site = Site()

    await _send(leasing, FetcherDispatch({"site": site}), Request(f"{SITE}/", session="alice"))

    assert site.sent == [("alice", "alice")]


async def test_cookies_set_during_a_fetch_are_saved_in_the_pool() -> None:
    leasing, pool, _, _ = _leasing()
    dispatch = FetcherDispatch({"site": Site(headers=[("Set-Cookie", "sid=abc")])})

    await _send(leasing, dispatch, Request(f"{SITE}/", session="alice"))

    lease = await pool.acquire(session="alice")
    assert lease is not None
    assert [(cookie.name, cookie.value) for cookie in (await pool.load_state(lease)).cookies] == [("sid", "abc")]


@pytest.mark.parametrize(
    ("headers", "cooling"),
    [([("Retry-After", "60")], 60.0), ([], 30.0), ([("Retry-After", "9999")], 300.0)],
    ids=["Retry-After", "default", "capped"],
)
async def test_a_429_cools_its_session_down(headers: list[tuple[str, str]], cooling: float) -> None:
    leasing, pool, stats, _ = _leasing()

    response = await _send(leasing, FetcherDispatch({"site": Site(429, headers)}), Request(f"{SITE}/", session="a"))

    assert response.status == 429
    assert pool.health("a") == "cooldown"
    assert await pool.ready_in(session="a") == pytest.approx(cooling, abs=1)
    assert (stats.get("sessions.outcomes.rate_limited"), stats.get("sessions.cooldowns")) == (1, 1)


@pytest.mark.parametrize(("error", "kind"), [(ProxyError("refused"), "proxy_failed"), (ConnectError("down"), "error")])
async def test_a_failed_fetch_reports_its_outcome(error: Exception, kind: str) -> None:
    leasing, pool, stats, _ = _leasing()
    site = Site()
    site.error = error

    with pytest.raises(type(error)):
        await _send(leasing, FetcherDispatch({"site": site}), Request(f"{SITE}/", session="a"))

    assert stats.get(f"sessions.outcomes.{kind}") == 1
    assert pool.health("a") == "available"


async def test_an_error_recorded_inside_the_block_is_the_outcome() -> None:
    leasing, _, stats, _ = _leasing()

    async with leasing.sending(Request(f"{SITE}/"), wait=0) as sending:
        sending.error = ProxyError("refused")

    assert stats.get("sessions.outcomes.proxy_failed") == 1


async def test_rendered_requests_lease_sessions_too() -> None:
    leasing, _, stats, _ = _leasing()

    async with leasing.sending(Request(f"{SITE}/", render=True), wait=0) as sending:
        assert sending.session is not None
        assert sending_session() == sending.session.id
    async with leasing.sending(Request(f"{SITE}/", render=True, session="a"), wait=0) as named:
        assert named.request.session == "a"

    assert stats.get("sessions.leased") == 2


async def test_a_fetch_nested_in_one_of_the_same_session_shares_its_lease() -> None:
    leasing, _, stats, _ = _leasing(max_concurrency=1)

    async with leasing.sending(Request(f"{SITE}/outer", session="a"), wait=0) as outer:
        assert sending_session() == "a"
        async with leasing.sending(Request(f"{SITE}/token", session="a"), wait=0) as nested:
            assert nested.session is outer.session

    assert stats.get("sessions.leased") == 1


async def test_a_fetch_in_another_task_does_not_share_the_lease() -> None:
    leasing, _, _, _ = _leasing(max_concurrency=1)

    async def elsewhere() -> None:
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass

    async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
        with pytest.raises(NoSessionYet) as busy:
            await asyncio.create_task(elsewhere())

    assert busy.value.delay == 0.0


async def test_no_session_in_time_tells_how_long_until_one_stops_cooling_down() -> None:
    leasing, _, _, clock = _leasing()
    dispatch = FetcherDispatch({"site": Site(429, [("Retry-After", "60")])})
    await _send(leasing, dispatch, Request(f"{SITE}/", session="a"))
    clock.now += 20

    with pytest.raises(NoSessionYet) as cooling:
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass

    assert cooling.value.delay == pytest.approx(40, abs=1)


class RealClock(Clock):
    def __call__(self) -> float:
        return time.time()


async def test_a_lease_is_renewed_while_its_fetch_runs() -> None:
    leasing, _, stats, _ = _leasing(clock=RealClock(), lease_ttl=0.3)

    async with leasing.sending(Request(f"{SITE}/"), wait=0):
        await asyncio.sleep(0.5)

    assert stats.get("sessions.stale") is None
    assert stats.get("sessions.outcomes.ok") == 1


async def test_changes_of_a_lease_that_expired_are_dropped_and_counted(caplog: pytest.LogCaptureFixture) -> None:
    leasing, pool, stats, clock = _leasing(lease_ttl=10)

    with caplog.at_level(logging.WARNING, logger="pernix.sessions.session_leasing"):
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0) as sending:
            assert sending.session is not None
            sending.session.cookies.receive(f"{SITE}/", ["late=1"], now=clock.now)
            clock.now += 11

    lease = await pool.acquire(session="a")
    assert lease is not None
    assert (await pool.load_state(lease)).cookies == []
    assert stats.get("sessions.stale") == 1
    assert "expired before its fetch" in caplog.text


async def test_a_fetcher_without_a_session_parameter_refuses_requests_under_one() -> None:
    class Plain:
        schemes: ClassVar[frozenset[str]] = frozenset({"https"})

        async def fetch(self, request: Request) -> Response:
            return make_response(request.url, request=request)

    dispatch = FetcherDispatch({"plain": Plain()})

    with sending_as(Session("a")), pytest.raises(SessionError, match="'plain' cannot send"):
        await dispatch(Request(f"{SITE}/"))
    assert (await dispatch(Request(f"{SITE}/"))).status == 200


async def test_a_429_a_nested_fetch_got_cools_the_session_down() -> None:
    leasing, pool, stats, _ = _leasing()

    async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0) as outer:
        async with leasing.sending(Request(f"{SITE}/token", session="a"), wait=0) as nested:
            nested.response = make_response(f"{SITE}/token", status=429, headers=[("Retry-After", "60")])
        outer.response = make_response(f"{SITE}/")  # the outer fetch handled it

    assert pool.health("a") == "cooldown"
    assert await pool.ready_in(session="a") == pytest.approx(60, abs=1)
    assert (stats.get("sessions.outcomes.ok"), stats.get("sessions.outcomes.rate_limited")) == (1, 1)
    assert stats.get("sessions.cooldowns") == 1


async def test_a_removal_is_timed_as_the_jar_removed_the_cookie() -> None:
    leasing, pool, _, clock = _leasing(max_concurrency=2)
    pool.add(Session("s", cookies=[Cookie(name="sid", value="old", domain="site.example", created=NOW, modified=NOW)]))

    async with leasing.sending(Request(f"{SITE}/", session="s"), wait=0) as first:
        assert first.session is not None
        clock.now = NOW + 1
        first.session.cookies.receive(f"{SITE}/", ["sid=; Max-Age=0"], now=clock.now)
        other = await pool.acquire(session="s")  # another fetch sets the cookie again, later
        assert other is not None
        renewed = Cookie(name="sid", value="new", domain="site.example", created=NOW, modified=NOW + 2)
        assert await pool.save_state(other, StateDelta(cookies=[renewed]))
        await pool.release(other, Outcome("ok"))
        clock.now = NOW + 4  # the first fetch ends after that

    lease = await pool.acquire(session="s")
    assert lease is not None
    assert [cookie.value for cookie in (await pool.load_state(lease)).cookies] == ["new"]


@pytest.mark.parametrize("held_at_start", [True, False])
async def test_a_removal_after_another_fetch_set_the_cookie_again_removes_it(held_at_start: bool) -> None:
    leasing, pool, _, clock = _leasing(max_concurrency=2)
    old = Cookie(name="sid", value="old", domain="site.example", created=NOW, modified=NOW)
    pool.add(Session("s", cookies=[old] if held_at_start else []))

    async with leasing.sending(Request(f"{SITE}/", session="s"), wait=0) as first:
        assert first.session is not None
        if held_at_start:
            clock.now = NOW + 1
            first.session.cookies.receive(f"{SITE}/", ["sid=; Max-Age=0"], now=clock.now)
        other = await pool.acquire(session="s")  # another fetch sets the cookie, later
        assert other is not None
        renewed = Cookie(name="sid", value="new", domain="site.example", created=NOW, modified=NOW + 2)
        assert await pool.save_state(other, StateDelta(cookies=[renewed]))
        await pool.release(other, Outcome("ok"))
        clock.now = NOW + 3  # then a response of the first fetch removes it
        first.session.cookies.receive(f"{SITE}/", ["sid=; Max-Age=0"], now=clock.now)

    lease = await pool.acquire(session="s")
    assert lease is not None
    assert (await pool.load_state(lease)).cookies == []


async def test_a_session_logs_in_before_its_first_fetch_with_its_account_sharing_the_lease() -> None:
    dispatch = FetcherDispatch({"site": Site(headers=[("Set-Cookie", "sid=1")])})
    logins: list[tuple[str | None, str | None]] = []

    async def log_in(session: Session, account: Account | None) -> None:
        logins.append((sending_session(), None if account is None else account.username))
        await _send(leasing, dispatch, Request(f"{SITE}/login", session=session.id))

    alice = Account(username="alice@shop.example", password="pw", id="alice")  # type: ignore[arg-type]
    leasing, pool, stats, _ = _leasing(login=_login(log_in, accounts=(alice,)), max_concurrency=2)
    pool.add(Session("alice", account="alice"))

    async with leasing.sending(Request(f"{SITE}/page"), wait=0) as sending:
        other = await pool.acquire(session="alice", timeout=0)  # the login ended, and its cookie was saved
        assert other is not None
        assert [cookie.name for cookie in (await pool.load_state(other)).cookies] == ["sid"]
        assert sending.request.session == "alice"
    await _send(leasing, dispatch, Request(f"{SITE}/page", session="alice"))

    assert logins == [("alice", "alice@shop.example")]
    assert (stats.get("sessions.logins"), stats.get("sessions.leased")) == (1, 2)  # the login shared its Lease


async def test_a_failed_login_cools_its_session_down_and_the_fetch_waits_for_another(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def log_in(session: Session, account: Account | None) -> None:
        raise RuntimeError("wrong password")

    leasing, pool, stats, _ = _leasing(login=_login(log_in))

    with (
        caplog.at_level(logging.ERROR, logger="pernix.sessions.session_leasing"),
        pytest.raises(NoSessionYet) as waiting,
    ):
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pytest.fail("the fetch went on without a login")

    assert waiting.value.delay == pytest.approx(30)
    assert pool.health("a") == "cooldown"
    assert (stats.get("sessions.login_failures"), stats.get("sessions.outcomes.login_failed")) == (1, 1)
    assert caplog.messages == ["Logging in the Session 'a' failed"]


async def test_a_session_retired_for_its_failed_logins_fails_the_requests_naming_it() -> None:
    async def log_in(session: Session, account: Account | None) -> None:
        raise RuntimeError("wrong password")

    leasing, _, _, clock = _leasing(login=_login(log_in), login_attempts=2)
    with pytest.raises(NoSessionYet):
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass
    clock.now += 30

    with pytest.raises(SessionError, match="retired"):
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass


async def test_a_response_logged_out_raises_login_expired_and_has_the_session_log_in_again() -> None:
    logins: list[str] = []

    async def log_in(session: Session, account: Account | None) -> None:
        logins.append(session.id)

    site = Site()
    leasing, pool, stats, _ = _leasing(login=_login(log_in))
    dispatch = FetcherDispatch({"site": site})
    await _send(leasing, dispatch, Request(f"{SITE}/", session="a"))
    site.status = 401

    with pytest.raises(LoginExpired) as expired:
        await _send(leasing, dispatch, Request(f"{SITE}/", session="a"))

    assert expired.value.response.status == 401
    assert expired.value.delay == 0
    assert stats.get("sessions.outcomes.auth_expired") == 1
    lease = await pool.acquire(session="a", timeout=0)
    assert lease is not None
    assert lease.must_log_in
    assert logins == ["a"]


async def test_a_fetch_logged_out_right_after_its_login_counts_a_failed_login(caplog: pytest.LogCaptureFixture) -> None:
    async def log_in(session: Session, account: Account | None) -> None:
        pass

    leasing, pool, stats, _ = _leasing(login=_login(log_in))

    with caplog.at_level(logging.WARNING, logger="pernix.sessions.session_leasing"), pytest.raises(LoginExpired):
        await _send(leasing, FetcherDispatch({"site": Site(401)}), Request(f"{SITE}/", session="a"))

    assert pool.health("a") == "cooldown"
    assert (stats.get("sessions.logins"), stats.get("sessions.login_failures")) == (1, 1)
    assert "the login counts as failed" in caplog.text


async def test_the_responses_of_a_login_are_not_judged() -> None:
    dispatch = FetcherDispatch({"site": Site(401)})

    async def log_in(session: Session, account: Account | None) -> None:
        response = await _send(leasing, dispatch, Request(f"{SITE}/login", session=session.id))
        assert response.status == 401

    leasing, _, stats, _ = _leasing(login=_login(log_in))

    async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
        pass

    assert stats.get("sessions.logins") == 1
    assert stats.get("sessions.outcomes.auth_expired") is None


async def test_a_judgment_that_raises_is_logged_and_the_session_taken_as_logged_in(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def log_in(session: Session, account: Account | None) -> None:
        pass

    def is_logged_out(response: Response) -> bool:
        raise ValueError("broken")

    leasing, _, stats, _ = _leasing(login=_login(log_in, is_logged_out))

    with caplog.at_level(logging.ERROR, logger="pernix.sessions.session_login"):
        response = await _send(leasing, FetcherDispatch({"site": Site()}), Request(f"{SITE}/", session="a"))

    assert response.status == 200
    assert stats.get("sessions.outcomes.ok") == 1
    assert "Judging whether the Session of" in caplog.text


async def test_a_cancelled_login_leaves_the_session_to_log_in() -> None:
    started = asyncio.Event()

    async def log_in(session: Session, account: Account | None) -> None:
        started.set()
        await asyncio.sleep(10)

    leasing, pool, _, _ = _leasing(login=_login(log_in))

    async def send() -> None:
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass

    task = asyncio.create_task(send())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    lease = await pool.acquire(session="a", timeout=0)
    assert lease is not None
    assert lease.must_log_in


PROXY_A, PROXY_B = "http://a.example:8080", "http://b.example:8080"


async def _proxies(pool: MemoryProxyPool, *urls: str) -> RunProxies:
    def mask(_: Iterable[str]) -> None:
        pass

    proxies = RunProxies(
        pool, [("proxies.urls", StaticProxies(urls))], refresh=None, stats=Stats(), stop=lambda: None, mask=mask
    )
    await proxies.open()
    return proxies


async def _state_proxy(pool: MemorySessionPool, session: str) -> str | None:
    lease = await pool.acquire(session=session, timeout=0)
    assert lease is not None
    try:
        return (await pool.load_state(lease)).proxy
    finally:
        await pool.release(lease, Outcome("ok"))


async def test_a_session_is_bound_to_a_proxy_it_keeps_and_the_binding_is_saved() -> None:
    proxies = await _proxies(MemoryProxyPool(), PROXY_A, PROXY_B)
    leasing, pool, _, _ = _leasing(proxies=proxies)
    bound: list[str | None] = []

    for session in ("a", "b", "a"):
        async with leasing.sending(Request(f"{SITE}/", session=session), wait=0) as sending:
            assert sending.session is not None
            bound.append(sending.session.proxy)

    assert bound == [PROXY_A, PROXY_B, PROXY_A]
    assert await _state_proxy(pool, "a") == PROXY_A


async def test_a_session_whose_proxy_failed_is_bound_to_another() -> None:
    proxy_pool = MemoryProxyPool()
    leasing, pool, stats, _ = _leasing(proxies=await _proxies(proxy_pool, PROXY_A, PROXY_B))
    site = Site()
    site.error = ProxyError("refused")

    with pytest.raises(ProxyError):
        await _send(leasing, FetcherDispatch({"site": site}), Request(f"{SITE}/", session="a"))
    site.error = None
    await _send(leasing, FetcherDispatch({"site": site}), Request(f"{SITE}/", session="a"))

    assert proxy_pool.health(PROXY_A) == "cooldown"
    assert await _state_proxy(pool, "a") == PROXY_B
    assert stats.get("sessions.outcomes.proxy_failed") == 1


async def test_with_no_proxy_available_the_lease_is_released_and_the_request_waits() -> None:
    proxy_pool = MemoryProxyPool(cooldown=30)
    leasing, pool, _, _ = _leasing(proxies=await _proxies(proxy_pool, PROXY_A))
    await proxy_pool.report(PROXY_A, failed=True, since=0)

    with pytest.raises(NoProxyYet) as waiting:
        async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
            pass

    assert waiting.value.delay == pytest.approx(30, abs=1)
    assert pool.health("a") == "available"
    assert await pool.acquire(session="a", timeout=0) is not None  # its Lease was released


async def test_a_login_goes_through_the_proxy_its_session_is_bound_to() -> None:
    seen: list[str | None] = []

    async def log_in(session: Session, account: Account | None) -> None:
        async with leasing.sending(Request(f"{SITE}/login", session=session.id), wait=0) as sending:
            assert sending.session is not None
            seen.append(sending.session.proxy)

    leasing, pool, _, _ = _leasing(login=_login(log_in), proxies=await _proxies(MemoryProxyPool(), PROXY_A))

    async with leasing.sending(Request(f"{SITE}/", session="a"), wait=0):
        pass

    assert seen == [PROXY_A]
    assert await _state_proxy(pool, "a") == PROXY_A


async def test_a_nested_fetch_reports_how_it_went_through_the_proxy() -> None:
    proxy_pool = MemoryProxyPool()
    leasing, _, _, _ = _leasing(proxies=await _proxies(proxy_pool, PROXY_A, PROXY_B))

    async with (
        leasing.sending(Request(f"{SITE}/outer", session="a"), wait=0),
        leasing.sending(Request(f"{SITE}/token", session="a"), wait=0) as nested,
    ):
        nested.error = ProxyError("refused")

    assert proxy_pool.health(PROXY_A) == "cooldown"


@pytest.mark.parametrize("own", ["http://own.example:1", ""], ids=["its own", "none"])
async def test_a_request_with_a_proxy_of_its_own_reports_nothing_against_the_sessions(own: str) -> None:
    proxy_pool = MemoryProxyPool()
    leasing, _, _, _ = _leasing(proxies=await _proxies(proxy_pool, PROXY_A))

    async with leasing.sending(Request(f"{SITE}/", session="a", proxy=own), wait=0) as sending:
        sending.error = ProxyError("refused")

    assert proxy_pool.health(PROXY_A) == "available"
