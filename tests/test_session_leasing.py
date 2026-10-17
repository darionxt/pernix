"""Leasing Sessions for fetches, against a fake Fetcher that sees the Session it is given."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Cookie, Request, Session
from pernix.errors import ConnectError, ProxyError, SessionError
from pernix.fetchers import FetcherDispatch
from pernix.fetchers.fetcher_dispatch import sending_as
from pernix.sessions import MemorySessionPool, Outcome, StateDelta
from pernix.sessions.session_leasing import NoSessionYet, SessionLeasing, sending_session, sending_with
from pernix.settings import SessionSettings
from pernix.stats import Stats
from pernix.testing import make_response

if TYPE_CHECKING:
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


def _leasing(*, clock: Clock | None = None, **settings: Any) -> tuple[SessionLeasing, MemorySessionPool, Stats, Clock]:
    clock, stats = clock or Clock(), Stats()
    config = SessionSettings(enabled=True, **settings)
    pool = MemorySessionPool(
        size=config.size, max_concurrency=config.max_concurrency, lease_ttl=config.lease_ttl, stats=stats, clock=clock
    )
    return SessionLeasing(pool, config, stats=stats, clock=clock), pool, stats, clock


async def _send(leasing: SessionLeasing, dispatch: FetcherDispatch, request: Request) -> Response:
    async with leasing.sending(request, wait=0) as sending:
        response = await dispatch(sending.request)
        sending.response = response
    return response


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


async def test_rendered_requests_are_sent_without_a_session() -> None:
    leasing, pool, stats, _ = _leasing()

    async with leasing.sending(Request(f"{SITE}/", render=True), wait=0) as sending:
        assert sending.session is None
        assert sending_session() is None
    with pytest.raises(SessionError, match="renders no Request under a Session"):
        async with leasing.sending(Request(f"{SITE}/", render=True, session="a"), wait=0):
            pass

    assert (len(pool), stats.get("sessions.leased")) == (0, None)


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


async def test_without_a_session_pool_requests_go_without_a_session() -> None:
    async with sending_with(None, Request(f"{SITE}/"), wait=0) as sending:
        assert (sending.session, sending.request.session) == (None, None)

    with pytest.raises(SessionError, match="no Session Pool is enabled"):
        sending_with(None, Request(f"{SITE}/", session="a"), wait=0)


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
