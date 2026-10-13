from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pernix.callbacks import iterate_and_close, run_errback
from pernix.errors import IgnoreRequest, RetriesExhausted, Retry
from pernix.events import RequestFailed, RequestIgnored, RequestRetried, ResponseReceived
from pernix.items import ItemContext, is_item
from pernix.request import Request
from pernix.response import expect_response
from pernix.retry_policy import Action, Decision

if TYPE_CHECKING:
    from pernix.events import EventBus
    from pernix.frontiers import Lease
    from pernix.middlewares import Download, HandleResponse
    from pernix.response import Response
    from pernix.retry_policy import RetryPolicy
    from pernix.spider import Spider
    from pernix.throttle import Throttle

__all__ = ["Again", "Dead", "Done", "PutItem", "RequestProcessor", "Settlement"]

logger = logging.getLogger(__name__)

type PutItem = Callable[[Any, ItemContext], Awaitable[None]]
"""Takes each Item a Callback or an errback produces; waiting in it suspends the producer."""

type Schedule = Callable[[Request], Awaitable[bool]]
"""Adds a Request to the Frontier; returns whether it was added."""


@dataclass(frozen=True, slots=True)
class Done:
    """Acknowledge the Lease."""


@dataclass(frozen=True, slots=True)
class Again:
    """Return the Request to the Frontier for another attempt."""

    delay: float
    reason: str
    count_attempt: bool = True


@dataclass(frozen=True, slots=True)
class Dead:
    """Keep the Request as a Dead Request."""

    reason: str


type Settlement = Done | Again | Dead
"""How to settle the Lease on a Request after an attempt."""


class RequestProcessor:
    """Makes one attempt at a claimed Request and tells how to settle its Lease.

    The Request waits for its slot in the Throttle; a slot cooling down sends it back
    to the Frontier without counting an attempt. It is fetched through the download
    chain while holding the slot, and the Response is handled through the spider
    chain, which ends in the Callback. The outputs are dispatched as they come:
    Requests are scheduled, and Items, recognized with `pernix.items.is_item()`, are
    handed to `put_item`.

    After every attempt that produced a Response, raised while fetching, or raised
    `Retry` in the Callback, the retry policy decides. A retried Request goes back to
    the Frontier with a delay (`RequestRetried`). A Request fails for good when its
    retries run out (with `RetriesExhausted`), when fetching raised an exception the
    policy does not retry, or when its Callback raised: `RequestFailed` is emitted,
    its errback runs, and it is to be kept as a Dead Request. Outputs a Callback
    yielded before raising are kept. `IgnoreRequest` raised while fetching is emitted
    as `RequestIgnored` and counts as done. Each Response handed to the spider chain
    is emitted as `ResponseReceived`.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        download: Download,
        handle: HandleResponse,
        schedule: Schedule,
        put_item: PutItem,
        events: EventBus,
        retry_policy: RetryPolicy,
        throttle: Throttle,
    ) -> None:
        """Wire the processor to its collaborators.

        Args:
            spider: The Spider whose `parse()` handles Requests without a Callback.
            download: Fetches a Request through the download Middleware and a Fetcher.
            handle: Runs the spider Middleware and the Callback for a Response.
            schedule: Adds the Requests the Callbacks and errbacks produce.
            put_item: Takes the Items the Callbacks and errbacks produce.
            events: Where Request and Response Events are emitted.
            retry_policy: Decides which attempts are retried.
            throttle: Paces fetches per slot.
        """
        self._spider = spider
        self._download = download
        self._handle = handle
        self._schedule = schedule
        self._put_item = put_item
        self._events = events
        self._retry_policy = retry_policy
        self._throttle = throttle

    async def attempt(self, lease: Lease) -> Settlement:
        """Fetch a Request and run its Callback once.

        Args:
            lease: The Lease on the Request.

        Returns:
            How to settle the Lease.
        """
        request = lease.request
        if not await self._throttle.acquire(lease.slot):
            # The domain is cooling down: wait in the Frontier, not in a worker.
            return Again(self._throttle.cooldown_left(lease.slot), "domain cooling down", count_attempt=False)
        try:
            response = await self._fetch(lease)
        except IgnoreRequest as exc:
            logger.debug("Ignored %r: %s", request, exc.reason)
            await self._events.emit(RequestIgnored(request, exc.reason))
            return Done()
        except Exception as exc:
            decision = self._decide(request, exc, lease.attempt)
            if decision.action is Action.ACCEPT:
                logger.error("Fetching %r failed", request, exc_info=exc)
                return await self._fail(request, exc)
            return await self._retry_or_give_up(lease, decision, cause=exc)

        decision = self._decide(request, response, lease.attempt)
        if decision.action is not Action.ACCEPT:
            return await self._retry_or_give_up(lease, decision, response=response)
        await self._events.emit(ResponseReceived(response))
        error = await self._handle_response(request, response)
        if error is None:
            return Done()
        if isinstance(error, Retry):
            decision = self._decide(request, error, lease.attempt)
            if decision.action is not Action.ACCEPT:
                return await self._retry_or_give_up(lease, decision, cause=error)
        return await self._fail(request, error)

    async def _fetch(self, lease: Lease) -> Response:
        """Fetch through the download chain in the slot `acquire()` gave the Lease, then free the slot."""
        try:
            response = expect_response(await self._download(lease.request), f"the download chain for {lease.request!r}")
            self._throttle.observe(lease.slot, response)
            return response
        finally:
            self._throttle.release(lease.slot)

    def _decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
        """Ask the retry policy; a policy that raises keeps the outcome."""
        try:
            return self._retry_policy.decide(request, outcome, attempt)
        except Exception:
            logger.exception("The retry policy failed to judge %r; keeping the outcome", request)
            return Decision.accept()

    async def _retry_or_give_up(
        self,
        lease: Lease,
        decision: Decision,
        *,
        response: Response | None = None,
        cause: Exception | None = None,
    ) -> Settlement:
        request = lease.request
        if decision.action is Action.RETRY:
            logger.info(
                "Retrying %r in %.2f s after attempt %d: %s", request, decision.delay, lease.attempt, decision.reason
            )
            await self._events.emit(RequestRetried(request, lease.attempt, decision.delay, decision.reason))
            return Again(decision.delay, decision.reason)
        logger.error("Gave up on %r after %d attempts: %s", request, lease.attempt, decision.reason)
        exhausted = RetriesExhausted(decision.reason, response=response)
        exhausted.__cause__ = cause
        return await self._fail(request, exhausted)

    async def _fail(self, request: Request, error: Exception) -> Settlement:
        """Report a Request that failed for good, then run its errback."""
        await self._events.emit(RequestFailed(request, error))
        if request.errback is not None:
            await self._run_errback(request, error)
        return Dead(_describe(error))

    async def _handle_response(self, request: Request, response: Response) -> Exception | None:
        """Run the spider chain for a Response and dispatch its outputs; return what it raised, if anything."""
        callback = request.callback or self._spider.parse
        async with aclosing(iterate_and_close(self._handle(response))) as outputs:
            while True:
                # Only advancing the Callback is isolated: a failure to store its
                # output is not the Callback's fault and must not be swallowed.
                try:
                    output = await anext(outputs)
                except StopAsyncIteration:
                    return None
                except Retry as exc:
                    return exc
                except Exception as exc:
                    logger.exception("Callback %s failed for %r", _name(callback), response)
                    return exc
                await self._handle_output(output, request, response)

    async def _run_errback(self, request: Request, error: Exception) -> None:
        async with aclosing(run_errback(request, error)) as outputs:
            while True:
                try:
                    output = await anext(outputs)
                except StopAsyncIteration:
                    return
                except Exception:
                    logger.exception("Errback %s failed for %r", _name(request.errback), request)
                    return
                await self._handle_output(output, request, None)

    async def _handle_output(self, output: object, request: Request, response: Response | None) -> None:
        match output:
            case None:
                pass
            case Request():
                await self._schedule(output)
            case _ if is_item(output):
                await self._put_item(output, ItemContext(request, response))
            case _:
                logger.warning("Ignoring unsupported output %r produced for %r", output, request)


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _name(callback: Callable[..., Any] | None) -> str:
    return getattr(callback, "__qualname__", repr(callback))
