"""Inline Fetch: fetching a Request in place, from a Callback, and awaiting its Response."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from pernix.errors import IgnoreRequest, RetriesExhausted
from pernix.events import RequestRetried
from pernix.fetchers import StreamedResponse
from pernix.fetchers.fetcher_dispatch import Streams, streaming_into
from pernix.frontiers import host_slot
from pernix.response import Response, expect_response
from pernix.retry_policy import Action, consult

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Generator

    from pernix.events import EventBus
    from pernix.middlewares import Download
    from pernix.request import Request
    from pernix.retry_policy import Decision, RetryPolicy
    from pernix.stats import Stats
    from pernix.throttle import Throttle

__all__ = ["InlineFetch", "handling"]

logger = logging.getLogger(__name__)

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
    `RequestRetried`. A Request without a Session continues the Session of the
    Request being handled, if it has one.

    A streamed fetch holds its slot until the stream is closed, and only the attempts
    up to the response headers are retried.
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
    ) -> None:
        """Wire the Inline Fetch to the Run's collaborators.

        Args:
            download: Fetches a Request through the download Middleware and a Fetcher.
            throttle: Paces fetches per slot.
            retry_policy: Decides which attempts are retried.
            events: Where `RequestRetried` is emitted.
            stats: Where streamed body bytes are counted, as `downloads.bytes`.
            slot_of: Computes the concurrency slot of a Request.
        """
        self._download = download
        self._throttle = throttle
        self._retry_policy = retry_policy
        self._events = events
        self._stats = stats
        self._slot_of = slot_of

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
            Exception: Fetching raised what the policy does not retry, such as a
                `FetchError`.
        """
        request = _continuing(request)
        attempt = 1
        while True:
            slot = self._slot_of(request)
            await self._acquire(slot)
            try:
                outcome = await self._attempt(request, slot, None)
            finally:
                self._throttle.release(slot)
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
            Exception: Fetching raised what the policy does not retry, such as a
                `FetchError`.
        """
        request = _continuing(request)
        attempt = 1
        while True:
            slot = self._slot_of(request)
            await self._acquire(slot)
            try:
                streams = Streams(max_bytes)
                try:
                    outcome = await self._attempt(request, slot, streams)
                    decision = consult(self._retry_policy, request, outcome, attempt)
                    if decision.action is Action.ACCEPT:
                        yield await self._streamed(_accepted(outcome), streams)
                        return
                finally:
                    await streams.close()
            finally:
                self._throttle.release(slot)
            await self._retry_later(request, outcome, decision, attempt)
            attempt += 1

    async def _acquire(self, slot: str) -> None:
        """Start a fetch in its slot, waiting out a cooldown in place."""
        while True:
            if await self._throttle.acquire(slot):
                return
            # The Throttle refuses a cooling slot rather than wait, as Frontier Requests wait in the Frontier.
            await asyncio.sleep(self._throttle.cooldown_left(slot))

    async def _attempt(self, request: Request, slot: str, streams: Streams | None) -> Response | Exception:
        """Fetch once through the download chain, streaming into `streams` if given; return the Response or the exception."""
        try:
            with streaming_into(streams):
                response = expect_response(await self._download(request), f"the download chain for {request!r}")
        except IgnoreRequest:
            raise
        except Exception as exc:
            return exc
        self._throttle.observe(slot, response)
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
    """The Request, continuing the Session of the Request being handled unless it has its own."""
    handled = _handled.get()
    if request.session is None and handled is not None and handled.session is not None:
        return request.replace(session=handled.session)
    return request


def _accepted(outcome: Response | Exception) -> Response:
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


async def _once(body: bytes) -> AsyncIterator[bytes]:
    if body:
        yield body
