from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pernix.callbacks import iterate_and_close, run_errback
from pernix.errors import IgnoreRequest, RetriesExhausted, Retry, SessionError
from pernix.events import RequestFailed, RequestIgnored, RequestRetried, ResponseReceived
from pernix.inline_fetch import handling
from pernix.items import ItemContext, is_item
from pernix.request import Request
from pernix.response import expect_response
from pernix.retry_policy import Action, consult
from pernix.sessions.session_leasing import NoSessionYet, sending_with
from pernix.throttle import COOLDOWN_STATUSES, SESSION_COOLDOWN_STATUSES

if TYPE_CHECKING:
    from pernix.events import EventBus
    from pernix.frontiers import Lease
    from pernix.middlewares import Download, HandleResponse
    from pernix.response import Response
    from pernix.retry_policy import Decision, RetryPolicy
    from pernix.sessions.session_leasing import SessionLeasing
    from pernix.spider import Spider
    from pernix.throttle import Throttle

__all__ = ["Again", "Dead", "Done", "PutItem", "RequestProcessor", "Settlement"]

logger = logging.getLogger(__name__)

type PutItem = Callable[[Any, ItemContext], Awaitable[None]]
"""Takes each Item a Callback or an errback produces; waiting in it suspends the producer."""

type Schedule = Callable[[Request], Awaitable[bool]]
"""Adds a Request to the Frontier; returns whether it was added."""

_SESSION_WAIT = 1.0
"""Seconds a worker waits for a busy Session before its Request goes back to the Frontier."""


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

    The Request takes its place in its slot in the Throttle; a slot cooling down
    sends it back to the Frontier without counting an attempt. With a Session Pool,
    it then leases a Session, the one it names or any, waiting a moment for a busy
    one; when every Session that fits is busy or cooling down, it goes back to the
    Frontier the same way, until one stops cooling down, and a Request no Session can
    ever be leased for fails. A Session that must log in does so first, in the
    Request's place. It then waits for the slot's delay and rate. It is fetched
    through the download chain while holding its Session and the slot, and the
    Response is handled through the spider chain, which ends in the Callback. A
    Response that shows the Session logged out raises `LoginExpired`, a `Retry`. Callbacks, errbacks, and Events get the Request as it
    was sent, naming its Session. The outputs are dispatched as they come:
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
        sessions: SessionLeasing | None = None,
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
            sessions: Leases Sessions from the Session Pool; `None` without one.
        """
        self._spider = spider
        self._download = download
        self._handle = handle
        self._schedule = schedule
        self._put_item = put_item
        self._events = events
        self._retry_policy = retry_policy
        self._throttle = throttle
        self._sessions = sessions

    async def attempt(self, lease: Lease) -> Settlement:
        """Fetch a Request and run its Callback once.

        Args:
            lease: The Lease on the Request.

        Returns:
            How to settle the Lease.
        """
        sent = _Sent(lease.request)
        try:
            response = await self._fetch(lease, sent)
        except NoSessionYet as wait:
            return Again(wait.delay, "no Session available", count_attempt=False)
        except SessionError as exc:
            logger.error("Cannot send %r: %s", sent.request, exc)
            return await self._fail(sent.request, exc)
        except IgnoreRequest as exc:
            logger.debug("Ignored %r: %s", sent.request, exc.reason)
            await self._events.emit(RequestIgnored(sent.request, exc.reason))
            return Done()
        except Exception as exc:
            decision = self._decide(sent.request, exc, lease.attempt)
            if decision.action is Action.ACCEPT:
                logger.error("Fetching %r failed", sent.request, exc_info=exc)
                return await self._fail(sent.request, exc)
            return await self._retry_or_give_up(lease, sent.request, decision, cause=exc)
        if response is None:
            # The domain is cooling down: wait in the Frontier, not in a worker.
            return Again(self._throttle.cooldown_left(lease.slot), "domain cooling down", count_attempt=False)

        request = sent.request  # naming its Session, for the Callback and the errback
        decision = self._decide(request, response, lease.attempt)
        if decision.action is not Action.ACCEPT:
            return await self._retry_or_give_up(lease, request, decision, response=response)
        await self._events.emit(ResponseReceived(response))
        error = await self._handle_response(request, response)
        if error is None:
            return Done()
        if isinstance(error, Retry):
            decision = self._decide(request, error, lease.attempt)
            if decision.action is not Action.ACCEPT:
                return await self._retry_or_give_up(lease, request, decision, cause=error)
        return await self._fail(request, error)

    async def _fetch(self, lease: Lease, sent: _Sent) -> Response | None:
        """Fetch through the download chain in its slot, under its Session; `None` if the slot is cooling down.

        The fetch takes its place in the slot, then leases its Session, logging it in
        if it must, then waits for the slot's delay and rate: it never holds a
        Session while it waits for a place, and its delay counts from the download,
        however long the Session took to free up. The fetches a login or a download
        Middleware makes share its place.

        Raises:
            pernix.errors.LoginExpired: The Response shows the Session logged out.
        """
        if not await self._throttle.admit(lease.slot):
            return None
        try:
            with self._throttle.holding(lease.slot):  # Inline Fetches a login or a Middleware makes share it
                async with sending_with(self._sessions, lease.request, wait=_SESSION_WAIT) as sending:
                    sent.request = sending.request
                    if not await self._throttle.pace(lease.slot):
                        return None
                    response = expect_response(
                        await self._download(sending.request), f"the download chain for {lease.request!r}"
                    )
                    statuses = COOLDOWN_STATUSES if sending.session is None else SESSION_COOLDOWN_STATUSES
                    self._throttle.observe(lease.slot, response, statuses=statuses)
                    sending.received(response)
        finally:
            self._throttle.release(lease.slot)
        return response

    def _decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
        return consult(self._retry_policy, request, outcome, attempt)

    async def _retry_or_give_up(
        self,
        lease: Lease,
        request: Request,
        decision: Decision,
        *,
        response: Response | None = None,
        cause: Exception | None = None,
    ) -> Settlement:
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
        with handling(response.request):
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
        with handling(request):
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


@dataclass(slots=True)
class _Sent:
    """The Request one attempt sends: the claimed one, naming its Session once one is leased."""

    request: Request


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _name(callback: Callable[..., Any] | None) -> str:
    return getattr(callback, "__qualname__", repr(callback))
