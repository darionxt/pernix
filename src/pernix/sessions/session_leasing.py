"""Sending Requests under the Sessions of a Session Pool, one Lease per fetch."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.errors import IgnoreRequest, LoginExpired, ProxyError, SessionError
from pernix.fetchers.fetcher_dispatch import BROWSER_FETCHER, sending_as
from pernix.retry_policy import retry_after_seconds
from pernix.session import Session, SessionState
from pernix.sessions.session_pool import Outcome, OutcomeKind, StateDelta

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from pernix.request import Request
    from pernix.response import Response
    from pernix.sessions.session_login import SessionLogin
    from pernix.sessions.session_pool import SessionLease, SessionPool
    from pernix.settings import SessionSettings
    from pernix.stats import Stats

__all__ = ["NoSessionYet", "Sending", "SessionLeasing", "sending_session", "sending_with"]

logger = logging.getLogger(__name__)

_SEVERITY: tuple[OutcomeKind, ...] = (
    "rate_limited",
    "blocked",
    "login_failed",
    "auth_expired",
    "proxy_failed",
    "error",
    "ok",
)
"""The outcomes, the one a Lease is released with first, when the fetches sharing it fared differently."""


@dataclass(slots=True)
class _Held:
    """A Session a fetch holds, and the outcomes of the fetches nested in it that share its Lease."""

    leasing: SessionLeasing
    session: Session
    task: asyncio.Task[Any] | None
    logging_in: bool = False
    """Whether the fetch is logging the Session in, so the Responses of the fetches nested in it are not judged."""
    nested: list[Outcome] = field(default_factory=list[Outcome])


_held: ContextVar[tuple[_Held, ...]] = ContextVar("pernix_held_sessions", default=())
"""The Sessions held by the fetches the current code runs inside of, innermost last."""


class NoSessionYet(Exception):
    """No Session could be leased in time, or the one leased failed to log in; the Request is to wait elsewhere.

    Attributes:
        delay: Seconds until a Session that fits stops cooling down; 0 if one is
            only busy.
    """

    def __init__(self, delay: float) -> None:
        """Create the signal.

        Args:
            delay: Seconds to wait.
        """
        super().__init__(f"no Session could be leased; one may be in {delay:.2f} s")
        self.delay = delay


@dataclass(slots=True)
class Sending:
    """A Request on its way, under the Session leased for it, if any.

    Attributes:
        request: The Request, naming the Session it is sent under.
        session: The Session; `None` for a Request sent without one.
        response: The Response, once it arrived, which tells how the Session fared.
        error: The exception the fetch failed with, for a caller that handles it
            inside the block, which tells how the Session fared too.
        is_logged_out: Whether a Response shows the Session logged out; `None`
            when no Response is judged, as without a login, or during one.
    """

    request: Request
    session: Session | None = None
    response: Response | None = None
    error: BaseException | None = None
    is_logged_out: Callable[[Response], bool] | None = field(default=None, repr=False)

    def received(self, response: Response) -> None:
        """Record the Response the fetch got.

        Args:
            response: The Response.

        Raises:
            pernix.errors.LoginExpired: The Response shows the Session logged out;
                it is recorded as the `error` too.
        """
        self.response = response
        if self.is_logged_out is not None and self.is_logged_out(response):
            self.error = LoginExpired(response)
            raise self.error


class SessionLeasing:
    """Leases a Session from a Session Pool for each fetch, and reports how it fared.

    A Request naming a Session leases that one; any other leases whichever the pool
    gives. Rendered Requests are sent without a Session, as the browser takes none,
    and one naming a Session is refused. The fetch runs under a Session built from
    the state the pool holds, its Lease renewed meanwhile; then the changes it made
    are saved and the Lease is released with its outcome: `rate_limited` for a 429,
    until its `Retry-After` or for `sessions.cooldown` seconds, at most
    `sessions.cooldown_max`; `auth_expired` for a Response the login judges logged
    out; `proxy_failed` for a `ProxyError`; `error` for other exceptions; and `ok`
    otherwise. A fetch nested in another of the same Session, in the same task, such
    as one a download Middleware makes, shares the outer fetch's Lease and Session;
    the Lease is released with the most severe outcome of them all, so a 429 one of
    them got cools the Session down.

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
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the leasing to its pool.

        Args:
            pool: The Session Pool.
            settings: The `sessions.*` settings.
            stats: Where Leases, logins, and outcomes are counted.
            login: Logs Sessions in, and judges the Responses fetched under them;
                `None` when Sessions do not log in.
            clock: The current time, in seconds since the epoch.
        """
        self.pool = pool
        self._settings = settings
        self._stats = stats
        self._login = login
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
            pernix.errors.SessionError: No Session can ever be leased for the
                Request, or it is rendered but names a Session.
        """
        if _rendered(request):
            if request.session is not None:
                raise SessionError(f"the browser renders no Request under a Session, and {request!r} names one")
            with sending_as(None):
                yield Sending(request)
            return
        held = _held_by(self, request.session)
        if held is not None:
            shared = Sending(request, held.session, is_logged_out=self._judge(held))
            failure: BaseException | None = None
            try:
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
        except BaseException:
            await self.pool.release(lease, Outcome("ok"))
            raise
        session = Session.from_state(lease.session, before)
        renewing = asyncio.create_task(self._renew(lease))
        holding = _Held(self, session, asyncio.current_task(), logging_in=lease.must_log_in)
        token = _held.set((*_held.get(), holding))
        try:
            if lease.must_log_in:
                before = await self._log_in(lease, before, holding, request)
            sending = Sending(request.replace(session=lease.session), session, is_logged_out=self._judge(holding))
            error: BaseException | None = None
            try:
                with sending_as(session):
                    yield sending
            except BaseException as exc:
                error = exc
                raise
            finally:
                outcomes = [self._outcome(sending.response, error or sending.error), *holding.nested]
                await self._finish(lease, before, sending, session, outcomes)
        finally:
            _held.reset(token)
            renewing.cancel()

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
            await self._fail_login(lease, holding.nested)
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

    async def _fail_login(self, lease: SessionLease, nested: list[Outcome]) -> None:
        """Release the Lease of a Session whose login failed, cooling it down."""
        cooldowns = [outcome.until for outcome in nested if outcome.kind == "rate_limited" and outcome.until]
        outcome = Outcome("login_failed", until=max([self._clock() + self._settings.cooldown, *cooldowns]))
        released = await self.pool.release(lease, outcome)
        self._stats.inc("sessions.login_failures")
        for each in (outcome, *nested):
            self._stats.inc(f"sessions.outcomes.{each.kind}")
        if not released:
            self._stats.inc("sessions.stale")

    async def _finish(
        self, lease: SessionLease, before: SessionState, sending: Sending, session: Session, outcomes: list[Outcome]
    ) -> None:
        """Save the changes the fetches made to their Session, then release the Lease with the most telling outcome.

        The cookies removed are timed as the jar removed them, so a cookie another
        fetch set later is kept. A fetch logged out right after the login it made
        reports the login as failed.
        """
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
        released = await self.pool.release(lease, outcome)
        for each in outcomes:
            self._stats.inc(f"sessions.outcomes.{each.kind}")
        if outcome.kind == "rate_limited":
            self._stats.inc("sessions.cooldowns")
        if not (saved and released):
            self._stats.inc("sessions.stale")
            logger.warning(
                "The Lease on the Session %r expired before its fetch of %r ended; its changes are lost",
                lease.session,
                sending.request,
            )

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


def sending_with(
    leasing: SessionLeasing | None, request: Request, *, wait: float | None
) -> AbstractAsyncContextManager[Sending]:
    """Send a Request under a Session leased for it inside the block, or under none without a Session Pool.

    Args:
        leasing: The Run's Session leasing; `None` without a Session Pool.
        request: The Request.
        wait: Seconds to wait for a Session; `None` to wait as long as it takes.

    Returns:
        An async context manager giving the Request as it is to be sent, and its
        Session; see `SessionLeasing.sending()`.

    Raises:
        pernix.errors.SessionError: The Request names a Session, but no Session
            Pool is enabled.
    """
    if leasing is not None:
        return leasing.sending(request, wait=wait)
    if request.session is not None:
        raise SessionError(
            f"{request!r} names the Session {request.session!r}, but no Session Pool is enabled; "
            "enable one with sessions.enabled"
        )
    return _without_session(request)


@asynccontextmanager
async def _without_session(request: Request) -> AsyncGenerator[Sending]:
    yield Sending(request)


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
    """The outcome to release a Lease with: the most severe, with the latest cooldown among those rate limited."""
    first = min(outcomes, key=lambda outcome: _SEVERITY.index(outcome.kind))
    untils = [outcome.until for outcome in outcomes if outcome.kind == first.kind and outcome.until is not None]
    return Outcome(first.kind, until=max(untils) if untils else None)


def _rendered(request: Request) -> bool:
    return bool(request.render) or request.fetcher == BROWSER_FETCHER
