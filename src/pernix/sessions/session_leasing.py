"""Sending Requests under the Sessions of a Session Pool, one Lease per fetch."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.errors import IgnoreRequest, LoginExpired, ProxyError, SessionError
from pernix.fetchers.fetcher_dispatch import sending_as
from pernix.proxies.run_proxies import FollowedProxy
from pernix.retry_policy import retry_after_seconds
from pernix.sending import NotYet, Sending
from pernix.session import Session, SessionState
from pernix.sessions.session_pool import Outcome, OutcomeKind, StateDelta

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable
    from contextlib import AbstractAsyncContextManager

    from pernix.account import Account
    from pernix.proxies.run_proxies import RunProxies
    from pernix.request import Request
    from pernix.response import Response
    from pernix.sessions.session_login import SessionLogin
    from pernix.sessions.session_pool import SessionLease, SessionPool
    from pernix.settings import SessionSettings
    from pernix.stats import Stats

__all__ = ["NoSessionYet", "SessionLeasing", "sending_session"]

logger = logging.getLogger(__name__)

_SEVERITY: tuple[OutcomeKind, ...] = (
    "proxy_retired",
    "rate_limited",
    "blocked",
    "login_failed",
    "auth_expired",
    "proxy_cooldown",
    "proxy_failed",
    "error",
    "ok",
)
"""The outcomes, the one a Lease is released with first, when the fetches sharing it fared differently."""
_RETIRING: frozenset[OutcomeKind] = frozenset({"login_failed", "blocked", "proxy_retired"})
"""The outcomes a Session Pool may Retire a Session for."""


@dataclass(slots=True)
class _Held:
    """A Session a fetch holds, and the outcomes of the fetches nested in it that share its Lease."""

    leasing: SessionLeasing
    session: Session
    task: asyncio.Task[Any] | None
    logging_in: bool = False
    """Whether the fetch is logging the Session in, so the Responses of the fetches nested in it are not judged."""
    proxy: str | None = None
    """The proxy the Lease bound its Session to, which its later fetches keep, and which it holds until it ends."""
    nested: list[Outcome] = field(default_factory=list[Outcome])


_held: ContextVar[tuple[_Held, ...]] = ContextVar("pernix_held_sessions", default=())
"""The Sessions held by the fetches the current code runs inside of, innermost last."""


class NoSessionYet(NotYet):
    """No Session could be leased in time, or the one leased failed to log in; the Request is to wait elsewhere."""

    def __init__(self, delay: float) -> None:
        """Create the signal.

        Args:
            delay: Seconds until a Session that fits stops cooling down; 0 if one
                is only busy.
        """
        super().__init__(delay, "no Session available")


class SessionLeasing:
    """Leases a Session from a Session Pool for each fetch, and reports how it fared.

    A Request naming a Session leases that one; any other leases whichever the pool
    gives, rendered or not. The fetch runs under a Session built from
    the state the pool holds, its Lease renewed meanwhile; then the changes it made
    are saved and the Lease is released with its outcome: `rate_limited` for a 429,
    until its `Retry-After` or for `sessions.cooldown` seconds, at most
    `sessions.cooldown_max`; `auth_expired` for a Response the login judges logged
    out; `proxy_failed` for a `ProxyError`; `error` for other exceptions; and `ok`
    otherwise. A fetch nested in another of the same Session, in the same task, such
    as one a download Middleware makes, shares the outer fetch's Lease and Session;
    the Lease is released with the most severe outcome of them all, so a 429 one of
    them got cools the Session down.

    With the Run's proxies, a Session goes through the proxy the binding policy
    binds it to, by default the one it is bound to while that is available. Each
    Lease settles the Session's proxy once, before its first fetch that goes through
    it, which may be one of its login or one nested in another, and its later
    fetches keep it, so each is reported against the proxy it went through. The
    Lease holds the proxy until it ends, so a Session an overlapping Lease bound to
    another still counts toward it meanwhile. The binding is saved with the
    Session's state, and released once the pool Retires the Session. A Request with
    a proxy of its own goes through that, and needs none of the Run's. When no proxy
    is available, or none fits the Session, the Lease is released, and the Request
    waits elsewhere; a nested fetch waits as its caller does.

    A Session the policy keeps on a proxy that cools down follows it: its Lease is
    released as `proxy_cooldown`, or, after a fetch through the proxy failed, as
    `proxy_failed`, cooling the Session down until the proxy is available, while
    the Request waits for a Session as for a busy one. Once its proxy is Retired,
    or no longer listed, the Lease is released as `proxy_retired`, Retiring the
    Session with it; the Request then waits for another, and one naming it fails
    with `SessionError`.

    With a login, a fetch whose Lease must log its Session in runs the login first,
    in the fetch's task, where the fetches the login makes share the Lease and are
    not judged; the cookies it set are saved, and the pool told, before the fetch goes
    on. A login that raises is logged, and the Lease released as `login_failed`,
    cooling the Session down for `sessions.cooldown` seconds; the fetch then waits
    for another Session, as for a busy one. A fetch logged out right after the login
    it made counts as a failed login too, so a login that does not work is not
    repeated past `sessions.login_attempts`.
    """

    def __init__(
        self,
        pool: SessionPool,
        settings: SessionSettings,
        *,
        stats: Stats,
        login: SessionLogin | None = None,
        proxies: RunProxies | None = None,
        accounts: Iterable[Account] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the leasing to its pool.

        Args:
            pool: The Session Pool.
            settings: The `sessions.*` settings.
            stats: Where Leases, logins, and outcomes are counted.
            login: Logs Sessions in, and judges the Responses fetched under them;
                `None` when Sessions do not log in.
            proxies: The Run's proxies, which Sessions are bound to; `None` without
                any.
            accounts: The Accounts Sessions are bound to, which the binding policy
                is told of.
            clock: The current time, in seconds since the epoch.
        """
        self.pool = pool
        self._settings = settings
        self._stats = stats
        self._login = login
        self._proxies = proxies
        self._accounts = {account.id: account for account in accounts}
        self._clock = clock

    @asynccontextmanager
    async def sending(self, request: Request, *, wait: float | None) -> AsyncGenerator[Sending]:
        """Lease a Session for a Request, logging it in if it must, and send the Request under it inside the block.

        Call `received()` on what this yields once the Response arrived. The
        Session's changes are saved, and its Lease released, on exit.

        Args:
            request: The Request.
            wait: Seconds to wait for a Session; `None` to wait as long as it takes.

        Yields:
            The Request as it is to be sent, and its Session.

        Raises:
            NoSessionYet: No Session could be leased within `wait`, or the one
                leased failed to log in.
            pernix.proxies.run_proxies.NoProxyYet: The Request goes through its
                Session's proxy, and no proxy is available, or none fits the
                Session.
            pernix.errors.SessionError: No Session can ever be leased for the
                Request.
        """
        held = _held_by(self, request.session)
        if held is not None:
            if held.proxy is None:
                held.proxy = await self._bind_nested(held, request)
            shared = Sending(request, held.session, is_logged_out=self._judge(held), nested=True)
            failure: BaseException | None = None
            try:
                async with self._reporting(shared):
                    with sending_as(held.session):
                        yield shared
            except BaseException as exc:
                failure = exc
                raise
            finally:
                held.nested.append(self._outcome(shared.response, failure or shared.error))
            return
        lease = await self.pool.acquire(session=request.session, timeout=wait)
        if lease is None:
            raise NoSessionYet(await self.pool.ready_in(session=request.session))
        self._stats.inc("sessions.leased")
        try:
            before = await self.pool.load_state(lease)
            session = Session.from_state(lease.session, before)
            bound = await self._bind_proxy(session, request)
        except FollowedProxy as followed:
            await self._follow(lease, followed)
            raise NoSessionYet(await self.pool.ready_in(session=request.session)) from followed
        except BaseException:
            await self.pool.release(lease, Outcome("ok"))
            raise
        renewing = asyncio.create_task(self._renew(lease))
        holding = _Held(self, session, asyncio.current_task(), logging_in=lease.must_log_in, proxy=bound)
        token = _held.set((*_held.get(), holding))
        try:
            if lease.must_log_in:
                before = await self._log_in(lease, before, holding, request)
            sending = Sending(request.replace(session=lease.session), session, is_logged_out=self._judge(holding))
            error: BaseException | None = None
            try:
                async with self._reporting(sending):
                    with sending_as(session):
                        yield sending
            except BaseException as exc:
                error = exc
                raise
            finally:
                outcomes = [self._outcome(sending.response, error or sending.error), *holding.nested]
                await self._finish(lease, before, sending, holding, outcomes)
        finally:
            _held.reset(token)
            renewing.cancel()
            if self._proxies is not None and holding.proxy is not None:
                await self._proxies.release(session.id, holding.proxy)

    async def _log_in(
        self, lease: SessionLease, before: SessionState, holding: _Held, request: Request
    ) -> SessionState:
        """Log in the Session a Lease must log in, and save what the login set; return the state it left.

        Raises:
            NoSessionYet: The login failed; the Lease was released, and the Request
                is to wait for another Session.
            pernix.errors.SessionError: The login failed, and no Session can ever be
                leased for the Request now.
        """
        session = holding.session
        try:
            if self._login is not None:
                await self._login.log_in(session)
        except Exception as exc:
            logger.error("Logging in the Session %r failed", lease.session, exc_info=exc)
            await self._fail_login(lease, holding)
            raise NoSessionYet(await self.pool.ready_in(session=request.session)) from exc
        except BaseException:
            await self.pool.release(lease, Outcome("ok"))
            raise
        finally:
            holding.logging_in = False
        after = session.state()
        delta = StateDelta.between(before, after, now=self._clock(), removed_at=session.cookies.removals)
        if (delta.empty or await self.pool.save_state(lease, delta)) and await self.pool.logged_in(lease):
            self._stats.inc("sessions.logins")
            logger.info("Logged in the Session %r", lease.session)
        return after

    async def _fail_login(self, lease: SessionLease, holding: _Held) -> None:
        """Release the Lease of a Session whose login failed, cooling it down, or Retiring it with its proxy.

        A Session that follows the proxy a fetch of the login failed through cools
        down, or is Retired, with it, as after any fetch.
        """
        nested = holding.nested
        cooldowns = [outcome.until for outcome in nested if outcome.until is not None]
        failed = Outcome("login_failed", until=max([self._clock() + self._settings.cooldown, *cooldowns]))
        followed = await self._follows(holding, nested)
        outcome = failed if followed is None else _with_proxy(failed, followed)
        if any(each.kind == "proxy_retired" for each in nested):  # a fetch of the login that would bind it found so
            outcome = Outcome("proxy_retired")
        released = await self._release(lease, outcome)
        self._stats.inc("sessions.login_failures")
        self._count([failed, *nested], outcome)
        if not released:
            self._stats.inc("sessions.stale")

    async def _finish(
        self, lease: SessionLease, before: SessionState, sending: Sending, holding: _Held, outcomes: list[Outcome]
    ) -> None:
        """Save the changes the fetches made to their Session, then release the Lease with the most telling outcome.

        The cookies removed are timed as the jar removed them, so a cookie another
        fetch set later is kept. A fetch logged out right after the login it made
        reports the login as failed. A Session that follows the proxy a fetch of
        the Lease failed through cools down, or is Retired, with it.
        """
        session = holding.session
        now = self._clock()
        delta = StateDelta.between(before, session.state(), now=now, removed_at=session.cookies.removals)
        saved = delta.empty or await self.pool.save_state(lease, delta)
        outcome = _most_telling(outcomes)
        if lease.must_log_in and outcome.kind == "auth_expired":
            logger.warning(
                "The Session %r was logged out right after logging in; the login counts as failed", lease.session
            )
            self._stats.inc("sessions.login_failures")
            outcome = Outcome("login_failed", until=now + self._settings.cooldown)
        followed = await self._follows(holding, outcomes)
        if followed is not None:
            outcome = _with_proxy(outcome, followed)
        released = await self._release(lease, outcome)
        self._count(outcomes, outcome)
        if outcome.kind == "rate_limited":
            self._stats.inc("sessions.cooldowns")
        if not (saved and released):
            self._stats.inc("sessions.stale")
            logger.warning(
                "The Lease on the Session %r expired before its fetch of %r ended; its changes are lost",
                lease.session,
                sending.request,
            )

    def _count(self, outcomes: list[Outcome], released: Outcome) -> None:
        """Count the outcomes of the fetches of a Lease, and its Session's Retirement with its proxy, which none had."""
        for each in outcomes:
            self._stats.inc(f"sessions.outcomes.{each.kind}")
        if released.kind == "proxy_retired" and all(each.kind != "proxy_retired" for each in outcomes):
            self._stats.inc("sessions.outcomes.proxy_retired")

    async def _follow(self, lease: SessionLease, followed: FollowedProxy) -> None:
        """Release the Lease of a Session that follows its proxy, cooling it down with it, or Retiring it with it."""
        outcome = _proxy_outcome(followed)
        released = await self._release(lease, outcome)
        self._stats.inc(f"sessions.outcomes.{outcome.kind}")
        if not released:
            self._stats.inc("sessions.stale")

    async def _follows(self, holding: _Held, outcomes: list[Outcome]) -> FollowedProxy | None:
        """Whether a Session follows the proxy its Lease went through, once a fetch of the Lease failed through one.

        A binding policy that fails then is logged, and the Session left as it is.
        """
        if self._proxies is None or holding.proxy is None or all(each.kind != "proxy_failed" for each in outcomes):
            return None
        session = holding.session
        try:
            return await self._proxies.following(session.id, holding.proxy, account=self._account(session))
        except Exception as exc:
            logger.error("The binding policy failed for the Session %r, whose proxy failed", session.id, exc_info=exc)
            return None

    async def _release(self, lease: SessionLease, outcome: Outcome) -> bool:
        """Release a Lease, and the proxy binding of its Session, should the pool Retire it for the outcome."""
        released = await self.pool.release(lease, outcome)
        if self._proxies is not None and outcome.kind in _RETIRING:
            try:
                await self.pool.ready_in(session=lease.session)
            except SessionError:  # it is Retired
                await self._proxies.unbind(lease.session)
        return released

    async def _bind_proxy(self, session: Session, request: Request) -> str | None:
        """Bind a Session to a proxy for its Lease, unless the Request goes through its own; return the proxy bound.

        Raises:
            pernix.proxies.run_proxies.NoProxyYet: No proxy is available, or none
                fits the Session.
        """
        if self._proxies is None or request.proxy is not None:
            return None
        session.proxy = await self._proxies.bind(session.id, session.proxy, account=self._account(session))
        return session.proxy

    async def _bind_nested(self, held: _Held, request: Request) -> str | None:
        """Bind the Session a nested fetch shares to a proxy, for the Lease it shares; return the proxy bound.

        A Session that follows its proxy has its Lease released with it, once the
        fetch holding the Lease ends.

        Raises:
            pernix.proxies.run_proxies.FollowedProxy: The Session waits for its
                proxy, which cools down.
            pernix.errors.SessionError: The Session is Retired with its proxy.
            pernix.proxies.run_proxies.NoProxyYet: No proxy is available, or none
                fits the Session.
        """
        try:
            return await self._bind_proxy(held.session, request)
        except FollowedProxy as followed:
            outcome = _proxy_outcome(followed)
            if outcome not in held.nested:
                held.nested.append(outcome)
            if followed.until is None:
                raise SessionError(f"the Session {held.session.id!r} is retired with its proxy") from followed
            raise

    def _account(self, session: Session) -> Account | None:
        """The Account a Session logs in with, if any, which the binding policy is told of."""
        return None if session.account is None else self._accounts.get(session.account)

    def _reporting(self, sending: Sending) -> AbstractAsyncContextManager[None]:
        """Report how a fetch went through its Session's proxy, unless the Request names its own."""
        proxy = None if sending.session is None else sending.session.proxy
        if self._proxies is None or proxy is None or sending.request.proxy is not None:
            return nullcontext()
        return self._proxies.reporting(proxy, sending)

    def _judge(self, held: _Held) -> Callable[[Response], bool] | None:
        """How the Responses fetched under a held Session are judged logged out; `None` during its login, or without one."""
        if self._login is None or held.logging_in:
            return None
        return self._login.logged_out

    def _outcome(self, response: Response | None, error: BaseException | None) -> Outcome:
        if isinstance(error, LoginExpired):
            return Outcome("auth_expired")
        if isinstance(error, ProxyError):
            return Outcome("proxy_failed")
        if isinstance(error, Exception) and not isinstance(error, IgnoreRequest):
            return Outcome("error")
        if response is not None and response.status == 429:
            seconds = retry_after_seconds(response.headers.get("Retry-After"), now=dt.datetime.now(dt.UTC))
            wait = self._settings.cooldown if seconds is None or seconds <= 0 else seconds
            return Outcome("rate_limited", until=self._clock() + min(wait, self._settings.cooldown_max))
        return Outcome("ok")  # a cancelled or ignored fetch says nothing about the Session either

    async def _renew(self, lease: SessionLease) -> None:
        """Renew a Lease every third of `sessions.lease_ttl`, until cancelled."""
        while True:
            await asyncio.sleep(self._settings.lease_ttl / 3)
            if not await self.pool.renew(lease):
                return


def sending_session() -> str | None:
    """Return the id of the Session the fetch the current task runs inside of is sent under, if any.

    Returns:
        The id of the innermost one.
    """
    task = asyncio.current_task()
    return next((held.session.id for held in reversed(_held.get()) if held.task is task), None)


def _held_by(leasing: SessionLeasing, session: str | None) -> _Held | None:
    """The Session a fetch the current task runs inside of holds under `session`, if any."""
    if session is None:
        return None
    task = asyncio.current_task()
    return next(
        (
            held
            for held in reversed(_held.get())
            if held.leasing is leasing and held.session.id == session and held.task is task
        ),
        None,
    )


def _most_telling(outcomes: list[Outcome]) -> Outcome:
    """The outcome to release a Lease with: the most severe, cooling the Session down until the latest of them all."""
    first = min(outcomes, key=lambda outcome: _SEVERITY.index(outcome.kind))
    untils = [outcome.until for outcome in outcomes if outcome.until is not None]
    return Outcome(first.kind, until=max(untils) if untils else None)


def _proxy_outcome(followed: FollowedProxy) -> Outcome:
    """The outcome of a Lease whose Session follows its proxy, without a fetch through it."""
    if followed.until is None:
        return Outcome("proxy_retired")
    return Outcome("proxy_cooldown", until=followed.until)


def _with_proxy(outcome: Outcome, followed: FollowedProxy) -> Outcome:
    """The outcome of a Lease whose Session follows the proxy a fetch of it failed through."""
    if followed.until is None:
        return Outcome("proxy_retired")
    return Outcome(outcome.kind, until=followed.until if outcome.until is None else max(followed.until, outcome.until))
