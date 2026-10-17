"""Inline Fetch: fetching a Request in place, from a Callback, and awaiting its Response."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from pernix.errors import IgnoreRequest, LoginExpired, RetriesExhausted, SessionError
from pernix.events import RequestRetried
from pernix.fetchers import StreamedResponse
from pernix.fetchers.fetcher_dispatch import Streams, streaming_into
from pernix.frontiers import host_slot
from pernix.response import Response, expect_response
from pernix.retry_policy import Action, consult
from pernix.sessions.session_leasing import NoSessionYet, sending_session, sending_with
from pernix.throttle import COOLDOWN_STATUSES, SESSION_COOLDOWN_STATUSES

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator

    from pernix.events import EventBus
    from pernix.middlewares import Download
    from pernix.request import Request
    from pernix.retry_policy import Decision, RetryPolicy
    from pernix.sessions.session_leasing import Sending, SessionLeasing
    from pernix.stats import Stats
    from pernix.throttle import Throttle

__all__ = ["InlineFetch", "handling"]

logger = logging.getLogger(__name__)

_SESSION_WAIT = 1.0
"""Seconds a fetch holds its place in a slot while it waits for a busy Session."""

_SESSION_RETRY = 0.05
"""Seconds a fetch waits without its place before it takes the place again and waits for the Session anew."""

_handled: ContextVar[Request | None] = ContextVar("pernix_handled_request", default=None)
"""The Request whose Callback or errback is running, if any."""


@contextmanager
def handling(request: Request) -> Generator[None]:
    """Mark the code inside the block as handling a Request, whose Session Inline Fetches continue.

    The Engine wraps each Callback and errback in this.

    Args:
        request: The Request as it was sent, or as it failed.

    Yields:
        Nothing; the block runs with the Request in effect.
    """
    token = _handled.set(request)
    try:
        yield
    finally:
        _handled.reset(token)


class InlineFetch:
    """Fetches Requests in place, for Callbacks and components, outside the Frontier.

    The Requests are not deduplicated or stored, and their Callbacks never run. Each
    attempt waits for its slot in the Throttle, waiting out a cooldown in place, and
    goes through the download chain. The retry policy judges every attempt, as for
    Requests from the Frontier, but a retry waits in place for its delay, emitting
    `RequestRetried`. Each attempt takes its place in its slot, then, with a Session
    Pool, leases a Session, waiting for one in place, but giving up its place now and
    then meanwhile, then waits for the slot's delay and rate; a Session that must log
    in does so first, in the fetch's place. A Request without a Session continues
    the Session of the fetch it is nested in, or of the Request being handled, if it
    has one; a fetch nested in another of the same Session, in the same task, shares
    its Lease. A Response that shows the Session logged out is retried as for
    `Retry`, once the Session logged in again.

    A fetch nested in another of the same slot, in the same task, such as one a
    download Middleware makes, shares the outer fetch's place instead of waiting for
    another, though it still waits for the delay, the rate, and a cooldown. A
    streamed fetch holds its slot until the stream is closed, so the
    fetches made in its block share it too, as they share its Session; only the
    attempts up to the response headers are retried.
    """

    def __init__(
        self,
        *,
        download: Download,
        throttle: Throttle,
        retry_policy: RetryPolicy,
        events: EventBus,
        stats: Stats,
        slot_of: Callable[[Request], str] = host_slot,
        sessions: SessionLeasing | None = None,
    ) -> None:
        """Wire the Inline Fetch to the Run's collaborators.

        Args:
            download: Fetches a Request through the download Middleware and a Fetcher.
            throttle: Paces fetches per slot.
            retry_policy: Decides which attempts are retried.
            events: Where `RequestRetried` is emitted.
            stats: Where streamed body bytes are counted, as `downloads.bytes`.
            slot_of: Computes the concurrency slot of a Request.
            sessions: Leases Sessions from the Session Pool; `None` without one.
        """
        self._download = download
        self._throttle = throttle
        self._retry_policy = retry_policy
        self._events = events
        self._stats = stats
        self._slot_of = slot_of
        self._sessions = sessions

    async def fetch(self, request: Request) -> Response:
        """Fetch a Request, retrying in place, and return its Response.

        Args:
            request: The Request.

        Returns:
            The Response the retry policy accepted, whatever its status code.

        Raises:
            pernix.errors.RetriesExhausted: The policy retried the Request until no
                retry was left; it holds the last Response, or is caused by the last
                exception.
            pernix.errors.IgnoreRequest: A download Middleware dropped the Request.
            pernix.errors.SessionError: No Session can ever be leased for the
                Request, or it names one while no Session Pool is enabled.
            Exception: Fetching raised what the policy does not retry, such as a
                `FetchError`.
        """
        request = _continuing(request)
        attempt = 1
        while True:
            slot = self._slot_of(request)
            async with self._sending(request, slot) as sending:
                outcome = await self._attempt(sending, slot, None)
            decision = consult(self._retry_policy, request, outcome, attempt)
            if decision.action is Action.ACCEPT:
                return _accepted(outcome)
            await self._retry_later(request, outcome, decision, attempt)
            attempt += 1

    @asynccontextmanager
    async def stream(self, request: Request, *, max_bytes: int | None = None) -> AsyncGenerator[StreamedResponse]:
        """Fetch a Request, retrying in place until its headers are accepted, and read its body as it arrives.

        Args:
            request: The Request.
            max_bytes: Bytes the body may have at most; `None` for the Fetcher's own
                limit, and 0 for no limit.

        Yields:
            The Response the retry policy accepted, with its body to read once; the
            transfer is closed, and the slot freed, on exit.

        Raises:
            pernix.errors.RetriesExhausted: The policy retried the Request until no
                retry was left.
            pernix.errors.IgnoreRequest: A download Middleware dropped the Request.
            pernix.errors.SessionError: No Session can ever be leased for the
                Request, or it names one while no Session Pool is enabled.
            Exception: Fetching raised what the policy does not retry, such as a
                `FetchError`.
        """
        request = _continuing(request)
        attempt = 1
        while True:
            slot = self._slot_of(request)
            async with self._sending(request, slot) as sending:  # through the block too
                streams = Streams(max_bytes)
                try:
                    outcome = await self._attempt(sending, slot, streams)
                    decision = consult(self._retry_policy, request, outcome, attempt)
                    if decision.action is Action.ACCEPT:
                        yield await self._streamed(_accepted(outcome), streams)
                        return
                finally:
                    await streams.close()
            await self._retry_later(request, outcome, decision, attempt)
            attempt += 1

    @asynccontextmanager
    async def _sending(self, request: Request, slot: str) -> AsyncGenerator[Sending]:
        """Hold a place in a slot and the Request's Session, then wait for the slot's pace.

        The place is held while waiting for a busy Session only so long: then it is
        given up while the Session frees up, and taken again, as a fetch nested in
        the one holding the Session may need the place.
        """
        while True:
            async with AsyncExitStack() as held:
                await held.enter_async_context(self._placed(slot))
                try:
                    sending = await held.enter_async_context(sending_with(self._sessions, request, wait=_SESSION_WAIT))
                except NoSessionYet as busy:
                    later = busy.delay
                else:
                    await self._paced(slot)
                    yield sending
                    return
            await asyncio.sleep(max(later, _SESSION_RETRY))

    @asynccontextmanager
    async def _placed(self, slot: str) -> AsyncGenerator[None]:
        """Hold a place in a slot for a fetch, or share the place of the fetch it is nested in, waiting out a cooldown."""
        if self._throttle.holds(slot):
            yield
            return
        while True:
            if await self._throttle.admit(slot):
                break
            # The Throttle refuses a cooling slot rather than wait, as Frontier Requests wait in the Frontier.
            await asyncio.sleep(self._throttle.cooldown_left(slot))
        try:
            with self._throttle.holding(slot):
                yield
        finally:
            self._throttle.release(slot)

    async def _paced(self, slot: str) -> None:
        """Wait for a slot's delay and rate, and a cooldown, then start the fetch."""
        while True:
            if await self._throttle.pace(slot):
                return
            await asyncio.sleep(self._throttle.cooldown_left(slot))

    async def _attempt(self, sending: Sending, slot: str, streams: Streams | None) -> Response | Exception:
        """Fetch once through the download chain, streaming into `streams` if given; return the Response or the exception."""
        request = sending.request
        try:
            with streaming_into(streams):
                response = expect_response(await self._download(request), f"the download chain for {request!r}")
        except (IgnoreRequest, SessionError):
            raise
        except Exception as exc:
            sending.error = exc
            return exc
        statuses = COOLDOWN_STATUSES if sending.session is None else SESSION_COOLDOWN_STATUSES
        self._throttle.observe(slot, response, statuses=statuses)
        try:
            sending.received(response)
        except LoginExpired as exc:  # retried, once the Session logged in again
            return exc
        return response

    async def _retry_later(
        self, request: Request, outcome: Response | Exception, decision: Decision, attempt: int
    ) -> None:
        """Wait for the retry the policy decided, or raise `RetriesExhausted` when it gave up."""
        if decision.action is Action.GIVE_UP:
            response = outcome if isinstance(outcome, Response) else None
            raise RetriesExhausted(decision.reason, response=response) from (
                outcome if isinstance(outcome, Exception) else None
            )
        logger.info(
            "Retrying %r in place in %.2f s after attempt %d: %s", request, decision.delay, attempt, decision.reason
        )
        await self._events.emit(RequestRetried(request, attempt, decision.delay, decision.reason))
        await asyncio.sleep(decision.delay)

    async def _streamed(self, response: Response, streams: Streams) -> StreamedResponse:
        """The body of the Response the download chain returned, as it arrives.

        A Response marked `streamed` is read from the transfer opened last; any other,
        such as one a cache Middleware returned, from its own body.
        """
        if response.streamed:
            transfer = streams.latest
            if transfer is None:
                raise RuntimeError(
                    f"the download chain returned a streamed Response for {response.request!r} it did not open"
                )
            return StreamedResponse(
                response, lambda: self._counted(transfer.iter_bytes()), max_bytes=transfer.max_bytes
            )
        await streams.close()
        body = response.body
        return StreamedResponse(
            response.replace(body=b"", streamed=True), lambda: _once(body), max_bytes=streams.max_bytes or None
        )

    async def _counted(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in chunks:
            self._stats.inc("downloads.bytes", len(chunk))
            yield chunk


def _continuing(request: Request) -> Request:
    """The Request, continuing the Session of the fetch it is nested in, or of the Request being handled, unless it has its own."""
    if request.session is not None:
        return request
    handled = _handled.get()
    session = sending_session() or (handled.session if handled is not None else None)
    return request if session is None else request.replace(session=session)


def _accepted(outcome: Response | Exception) -> Response:
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


async def _once(body: bytes) -> AsyncIterator[bytes]:
    if body:
        yield body
