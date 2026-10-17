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

from pernix.errors import IgnoreRequest, ProxyError, SessionError
from pernix.fetchers.fetcher_dispatch import BROWSER_FETCHER, sending_as
from pernix.retry_policy import retry_after_seconds
from pernix.session import Session, SessionState
from pernix.sessions.session_pool import Outcome, OutcomeKind, StateDelta

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from pernix.request import Request
    from pernix.response import Response
    from pernix.sessions.session_pool import SessionLease, SessionPool
    from pernix.settings import SessionSettings
    from pernix.stats import Stats

__all__ = ["NoSessionYet", "Sending", "SessionLeasing", "sending_session", "sending_with"]

logger = logging.getLogger(__name__)

_SEVERITY: tuple[OutcomeKind, ...] = ("rate_limited", "blocked", "auth_expired", "proxy_failed", "error", "ok")
"""The outcomes, the one a Lease is released with first, when the fetches sharing it fared differently."""


@dataclass(slots=True)
class _Held:
    """A Session a fetch holds, and the outcomes of the fetches nested in it that share its Lease."""

    leasing: SessionLeasing
    session: Session
    task: asyncio.Task[Any] | None
    nested: list[Outcome] = field(default_factory=list[Outcome])


_held: ContextVar[tuple[_Held, ...]] = ContextVar("pernix_held_sessions", default=())
"""The Sessions held by the fetches the current code runs inside of, innermost last."""


class NoSessionYet(Exception):
    """No Session could be leased in time; the Request is to wait elsewhere for `delay` seconds.

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
    """

    request: Request
    session: Session | None = None
    response: Response | None = None
    error: BaseException | None = None


class SessionLeasing:
    """Leases a Session from a Session Pool for each fetch, and reports how it fared.

    A Request naming a Session leases that one; any other leases whichever the pool
    gives. Rendered Requests are sent without a Session, as the browser takes none,
    and one naming a Session is refused. The fetch runs under a Session built from
    the state the pool holds, its Lease renewed meanwhile; then the changes it made
    are saved and the Lease is released with its outcome: `rate_limited` for a 429,
    until its `Retry-After` or for `sessions.cooldown` seconds, at most
    `sessions.cooldown_max`; `proxy_failed` for a `ProxyError`; `error` for other
    exceptions; and `ok` otherwise. A fetch nested in another of the same Session,
    in the same task, such as one a download Middleware makes, shares the outer
    fetch's Lease and Session; the Lease is released with the most severe outcome of
    them all, so a 429 one of them got cools the Session down.
    """

    def __init__(
        self,
        pool: SessionPool,
        settings: SessionSettings,
        *,
        stats: Stats,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the leasing to its pool.

        Args:
            pool: The Session Pool.
            settings: The `sessions.*` settings.
            stats: Where Leases and outcomes are counted.
            clock: The current time, in seconds since the epoch.
        """
        self.pool = pool
        self._settings = settings
        self._stats = stats
        self._clock = clock

    @asynccontextmanager
    async def sending(self, request: Request, *, wait: float | None) -> AsyncGenerator[Sending]:
        """Lease a Session for a Request and send it under the Session inside the block.

        Set `response` on what this yields once the Response arrived. The Session's
        changes are saved, and its Lease released, on exit.

        Args:
            request: The Request.
            wait: Seconds to wait for a Session; `None` to wait as long as it takes.

        Yields:
            The Request as it is to be sent, and its Session.

        Raises:
            NoSessionYet: No Session could be leased within `wait`.
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
            shared = Sending(request, held.session)
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
        sending = Sending(request.replace(session=lease.session), session)
        renewing = asyncio.create_task(self._renew(lease))
        holding = _Held(self, session, asyncio.current_task())
        token = _held.set((*_held.get(), holding))
        error: BaseException | None = None
        try:
            with sending_as(session):
                yield sending
        except BaseException as exc:
            error = exc
            raise
        finally:
            _held.reset(token)
            renewing.cancel()
            outcomes = [self._outcome(sending.response, error or sending.error), *holding.nested]
            await self._finish(lease, before, sending, session, outcomes)

    async def _finish(
        self, lease: SessionLease, before: SessionState, sending: Sending, session: Session, outcomes: list[Outcome]
    ) -> None:
        """Save the changes the fetches made to their Session, then release the Lease with the most telling outcome.

        The cookies removed are timed as the jar removed them, so a cookie another
        fetch set later is kept.
        """
        now = self._clock()
        delta = StateDelta.between(before, session.state(), now=now, removed_at=session.cookies.removals)
        saved = delta.empty or await self.pool.save_state(lease, delta)
        outcome = _most_telling(outcomes)
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

    def _outcome(self, response: Response | None, error: BaseException | None) -> Outcome:
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
