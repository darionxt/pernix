from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pernix.callbacks import iterate_and_close, run_errback
from pernix.errors import IgnoreRequest, RetriesExhausted, Retry
from pernix.events import (
    RequestDropped,
    RequestFailed,
    RequestIgnored,
    RequestRetried,
    RequestScheduled,
    ResponseReceived,
    SpiderIdle,
)
from pernix.items import ItemContext, is_item
from pernix.logs import request_context
from pernix.request import Request
from pernix.response import expect_response
from pernix.retry_policy import Action, Decision, DefaultRetryPolicy
from pernix.settings import DomainSettings, RetrySettings
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from pernix.events import EventBus
    from pernix.frontiers import Frontier, Lease
    from pernix.middlewares import Download, HandleResponse
    from pernix.response import Response
    from pernix.retry_policy import RetryPolicy
    from pernix.spider import Spider

__all__ = ["Engine", "PutItem"]

logger = logging.getLogger(__name__)

type PutItem = Callable[[Any, ItemContext], Awaitable[None]]
"""Takes each Item a Callback or an errback produces; waiting in it suspends the producer."""


@dataclass(frozen=True, slots=True)
class _Done:
    """Acknowledge the Lease."""


@dataclass(frozen=True, slots=True)
class _Again:
    """Return the Request to the Frontier for another attempt."""

    delay: float
    reason: str
    count_attempt: bool = True


@dataclass(frozen=True, slots=True)
class _Dead:
    """Keep the Request as a Dead Request."""

    reason: str


type _Settlement = _Done | _Again | _Dead


class Engine:
    """Runs a fixed number of workers that claim Requests from the Frontier and process them.

    Each worker claims one Request at a time under a Lease, skipping the slots the
    Throttle blocks, renews the Lease while it works, fetches the Request through the
    download chain while holding its slot, and handles the Response through the
    spider chain, which ends in the Callback. A Request whose slot is cooling down
    goes back to the Frontier until the cooldown ends, without counting an attempt. Once the Callback's outputs
    have been added, the Lease is acknowledged. Because a Request stays leased until
    then, the Requests its Callback adds are always in the Frontier before it leaves.
    Items, recognized with `pernix.items.is_item()`, are handed to `put_item`, which
    may make the Callback wait.

    After every attempt that produced a Response, raised while fetching, or raised
    `Retry` in the Callback, the retry policy decides. A retried Request goes back to
    the Frontier with a delay (`RequestRetried`), so it holds no slot meanwhile. A
    Request fails for good when its retries run out (with `RetriesExhausted`), when
    fetching raised an exception the policy does not retry, or when its Callback
    raised: it is kept as a Dead Request, `RequestFailed` is emitted, and its errback
    runs. Outputs a Callback yielded before raising are kept. `IgnoreRequest` raised
    while fetching is emitted as `RequestIgnored` and counts as done. If the Run is
    cancelled, Requests in flight are returned to the Frontier without counting an
    attempt.

    The Run is idle once `start()` is exhausted and nothing is ready, delayed, or
    leased. Then `SpiderIdle` is emitted and `Spider.on_idle()` runs; the Run ends when
    it adds no Request. Every Request passes the `RequestScheduled` gate before
    entering the Frontier; a vetoed or duplicate Request is emitted as
    `RequestDropped`. Each Response handed to the spider chain is emitted as
    `ResponseReceived`.

    Requests constructed in the spider chain resolve relative URLs against the
    Response handled by the layer, or the Callback, that constructs them. While a
    Request is processed, its Frontier id is the `request_id` of the records logged.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        frontier: Frontier,
        download: Download,
        handle: HandleResponse,
        put_item: PutItem,
        events: EventBus,
        concurrency: int,
        lease_ttl: float = 60.0,
        retry_policy: RetryPolicy | None = None,
        throttle: Throttle | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Wire the Engine to its collaborators.

        Args:
            spider: The Spider whose Requests and Callbacks to run.
            frontier: Where pending Requests are stored.
            download: Fetches a Request through the download Middleware and a Fetcher.
            handle: Runs the spider Middleware and the Callback for a Response,
                yielding the outputs; it ends in `pernix.callbacks.run_callbacks()`.
            put_item: Takes every Item the Callbacks and errbacks produce, with
                where it came from.
            events: Where Request and Response Events are emitted.
            concurrency: The number of workers, which is also the maximum number of
                Requests processed at once.
            lease_ttl: Seconds a claimed Request stays leased without renewal; Leases
                are renewed every third of it.
            retry_policy: Decides which attempts are retried; defaults to
                `DefaultRetryPolicy` with the default `RetrySettings`.
            throttle: Paces fetches per slot; defaults to the default
                `DomainSettings`.
            worker_id: Identifies this Worker to the Frontier; defaults to the host
                name and process id.

        Raises:
            ValueError: `concurrency` is less than 1, or `lease_ttl` is not positive.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        if lease_ttl <= 0:
            raise ValueError(f"lease_ttl must be positive, got {lease_ttl}")
        self._spider = spider
        self._frontier = frontier
        self._download = download
        self._handle = handle
        self._put_item = put_item
        self._events = events
        self._concurrency = concurrency
        self._lease_ttl = lease_ttl
        self._retry_policy = retry_policy or DefaultRetryPolicy(RetrySettings())
        self._throttle = throttle or Throttle(DomainSettings())
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run(self) -> None:
        """Run until the Run is idle and `Spider.on_idle()` adds no more Requests."""
        async with asyncio.TaskGroup() as tasks:
            workers = [tasks.create_task(self._work()) for _ in range(self._concurrency)]
            await self._schedule_all(self._spider.start(), "start()")
            while True:
                await self._until_idle()
                await self._events.emit(SpiderIdle(self._spider))
                if not await self._schedule_all(self._spider.on_idle(), "on_idle()"):
                    break
            for worker in workers:  # all of them are waiting for work that will never come
                worker.cancel()

    async def _schedule_all(self, requests: AsyncIterator[Request], source: str) -> int:
        """Schedule the Requests a Spider method yields; return how many were added."""
        added = 0
        try:
            async with aclosing(iterate_and_close(requests)) as pending:
                async for request in pending:
                    added += await self._schedule(request)
        except Exception:
            logger.exception("%s of spider %r failed", source, self._spider.name)
        return added

    async def _until_idle(self) -> None:
        while (await self._frontier.counts()).unfinished:
            await self._frontier.wait()

    async def _work(self) -> None:
        while True:
            lease = await self._claim() or await self._wait_for_work()
            if lease is not None:
                with request_context(lease.id):  # records logged for the Request, its Callback's included
                    await self._process(lease)

    async def _claim(self) -> Lease | None:
        leases = await self._frontier.claim(self._worker_id, 1, self._lease_ttl, skip_slots=self._throttle.blocked())
        return leases[0] if leases else None

    async def _wait_for_work(self) -> Lease | None:
        """Wait until the Frontier changes or a blocked slot may start again; return a Lease found meanwhile."""
        loop = asyncio.get_running_loop()
        # Started eagerly, both waits register for changes before the claim below, so a
        # change made between an empty claim and the wait cannot go unnoticed.
        waits = [
            asyncio.Task(self._frontier.wait(), loop=loop, eager_start=True),
            asyncio.Task(self._throttle.wait(), loop=loop, eager_start=True),
        ]
        lease: Lease | None = None
        try:
            lease = await self._claim()
            if lease is None:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
            try:
                await asyncio.wait(waits)
            except asyncio.CancelledError:
                if lease is not None:  # claimed, but cancelled before it could be processed
                    await self._return(lease)
                raise
        return lease

    async def _process(self, lease: Lease) -> None:
        renewal = asyncio.create_task(self._keep_leased(lease))
        try:
            settlement = await self._attempt(lease)
            renewal.cancel()  # before settling, so a renewal never races the settlement
            settled = await self._settle(lease, settlement)
        except asyncio.CancelledError:
            # Until settled, the Request goes back; once settled, this write is stale and changes nothing.
            await self._return(lease)
            raise
        finally:
            renewal.cancel()
            await asyncio.wait([renewal])
        if not settled:
            logger.warning("The Lease on %r expired before it was settled; it may be processed again", lease.request)

    async def _keep_leased(self, lease: Lease) -> None:
        """Renew a Lease until cancelled, or until it is lost."""
        while True:
            await asyncio.sleep(self._lease_ttl / 3)
            try:
                renewed = await self._frontier.renew(lease, self._lease_ttl)
            except Exception:
                logger.exception("Renewing the Lease on %r failed", lease.request)
                return
            if renewed is None:
                logger.warning("Lost the Lease on %r while processing it; it may be processed again", lease.request)
                return

    async def _return(self, lease: Lease) -> None:
        """Give an unfinished Request back to the Frontier, without counting the attempt."""
        try:
            await self._frontier.nack(lease, count_attempt=False, reason="cancelled")
        except Exception:
            logger.exception("Returning %r to the Frontier failed", lease.request)

    async def _settle(self, lease: Lease, settlement: _Settlement) -> bool:
        match settlement:
            case _Again(delay, reason, count_attempt):
                return await self._frontier.nack(lease, delay=delay, count_attempt=count_attempt, reason=reason)
            case _Dead(reason):
                return await self._frontier.dead(lease, reason)
            case _Done():
                return await self._frontier.ack(lease)

    async def _attempt(self, lease: Lease) -> _Settlement:
        """Fetch a Request and run its Callback once; return how to settle its Lease."""
        request = lease.request
        if not await self._throttle.acquire(lease.slot):
            # The domain is cooling down: wait in the Frontier, not in a worker.
            return _Again(self._throttle.cooldown_left(lease.slot), "domain cooling down", count_attempt=False)
        try:
            response = await self._fetch(lease)
        except IgnoreRequest as exc:
            logger.debug("Ignored %r: %s", request, exc.reason)
            await self._events.emit(RequestIgnored(request, exc.reason))
            return _Done()
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
            return _Done()
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
    ) -> _Settlement:
        request = lease.request
        if decision.action is Action.RETRY:
            logger.info(
                "Retrying %r in %.2f s after attempt %d: %s", request, decision.delay, lease.attempt, decision.reason
            )
            await self._events.emit(RequestRetried(request, lease.attempt, decision.delay, decision.reason))
            return _Again(decision.delay, decision.reason)
        logger.error("Gave up on %r after %d attempts: %s", request, lease.attempt, decision.reason)
        exhausted = RetriesExhausted(decision.reason, response=response)
        exhausted.__cause__ = cause
        return await self._fail(request, exhausted)

    async def _fail(self, request: Request, error: Exception) -> _Settlement:
        """Report a Request that failed for good, then run its errback."""
        await self._events.emit(RequestFailed(request, error))
        if request.errback is not None:
            await self._run_errback(request, error)
        return _Dead(_describe(error))

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

    async def _schedule(self, request: Request) -> bool:
        veto = await self._events.gate(RequestScheduled(request))
        if veto is not None:
            await self._events.emit(RequestDropped(request, veto.reason))
            return False
        [added] = await self._frontier.add([request])
        if not added:
            await self._events.emit(RequestDropped(request, "duplicate"))
        return added

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
