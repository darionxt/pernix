from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from pernix.errors import IgnoreRequest
from pernix.events import (
    ItemError,
    ItemScraped,
    RequestDropped,
    RequestFailed,
    RequestIgnored,
    RequestScheduled,
    ResponseReceived,
    SpiderIdle,
)
from pernix.request import Request
from pernix.response import expect_response
from pernix.spider_middleware import resolving_against

if TYPE_CHECKING:
    from pernix.events import EventBus
    from pernix.frontier import Frontier, Lease
    from pernix.middleware import Download
    from pernix.response import Response
    from pernix.spider import Spider
    from pernix.spider_middleware import HandleResponse

__all__ = ["Engine", "ItemSink", "run_callbacks"]

logger = logging.getLogger(__name__)

type ItemSink = Callable[[Mapping[str, Any]], None]
"""Receives each Item a Callback produces."""


class Engine:
    """Runs a fixed number of workers that claim Requests from the Frontier and process them.

    Each worker claims one Request at a time under a Lease, renews the Lease while it
    works, fetches the Request through the download chain, and handles the Response
    through the spider chain, which ends in the Callback. Once the Callback's outputs
    have been added, the Lease is acknowledged; a Request that failed is kept as a Dead
    Request instead. Because a Request stays leased until then, the Requests its
    Callback adds are always in the Frontier before it leaves it.

    Exceptions raised while fetching or inside a Callback are logged, emitted as
    `RequestFailed`, and do not stop the Run; `IgnoreRequest` raised while fetching is
    emitted as `RequestIgnored` instead and counts as done. An exception raised while
    storing an Item ends the Run.

    The Run is idle once `start()` is exhausted and nothing is ready, delayed, or
    leased. Then `SpiderIdle` is emitted and `Spider.on_idle()` runs; the Run ends when
    it adds no Request. Every Request passes the `RequestScheduled` gate before
    entering the Frontier; a vetoed or duplicate Request is emitted as
    `RequestDropped`. Each fetched Response is emitted as `ResponseReceived`, each
    stored Item as `ItemScraped`, and an Item that fails to be stored as `ItemError`.

    Requests constructed in the spider chain resolve relative URLs against the
    Response handled by the layer, or the Callback, that constructs them.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        frontier: Frontier,
        download: Download,
        handle: HandleResponse,
        item_sink: ItemSink,
        events: EventBus,
        concurrency: int,
        lease_ttl: float = 60.0,
        worker_id: str | None = None,
    ) -> None:
        """Wire the Engine to its collaborators.

        Args:
            spider: The Spider whose Requests and Callbacks to run.
            frontier: Where pending Requests are stored.
            download: Fetches a Request through the download Middleware and a Fetcher.
            handle: Runs the spider Middleware and the Callback for a Response,
                yielding the outputs; it ends in `run_callbacks()`.
            item_sink: Receives every Item the Callbacks produce.
            events: Where Request, Response, and Item Events are emitted.
            concurrency: The number of workers, which is also the maximum number of
                Requests processed at once.
            lease_ttl: Seconds a claimed Request stays leased without renewal; Leases
                are renewed every third of it.
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
        self._item_sink = item_sink
        self._events = events
        self._concurrency = concurrency
        self._lease_ttl = lease_ttl
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    async def run(self) -> None:
        """Run until the Run is idle and `Spider.on_idle()` adds no more Requests.

        Raises:
            ExceptionGroup: Storing an Item failed; it holds the original exception.
        """
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
            async with aclosing(_iterate(requests)) as pending:
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
            leases = await self._frontier.claim(self._worker_id, 1, self._lease_ttl)
            if not leases:
                await self._frontier.wait()
                continue
            await self._process(leases[0])

    async def _process(self, lease: Lease) -> None:
        renewal = asyncio.create_task(self._keep_leased(lease))
        try:
            failure = await self._process_request(lease.request)
        finally:
            renewal.cancel()
            await asyncio.wait([renewal])
        settled = await (self._frontier.ack(lease) if failure is None else self._frontier.dead(lease, failure))
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

    async def _process_request(self, request: Request) -> str | None:
        """Fetch a Request and run its Callback; return why it failed, or `None`."""
        try:
            response = expect_response(await self._download(request), f"the download chain for {request!r}")
        except IgnoreRequest as exc:
            logger.debug("Ignored %r: %s", request, exc.reason)
            await self._events.emit(RequestIgnored(request, exc.reason))
            return None
        except Exception as exc:
            logger.exception("Fetching %r failed", request)
            await self._events.emit(RequestFailed(request, exc))
            return _describe(exc)
        await self._events.emit(ResponseReceived(response))
        return await self._handle_response(request, response)

    async def _handle_response(self, request: Request, response: Response) -> str | None:
        """Run the spider chain for a Response and dispatch its outputs; return why it failed, or `None`."""
        callback = request.callback or self._spider.parse
        async with aclosing(_iterate(self._handle(response))) as outputs:
            while True:
                # Only advancing the Callback is isolated: a failure to store its
                # output is not the Callback's fault and must not be swallowed.
                try:
                    output = await anext(outputs)
                except StopAsyncIteration:
                    return None
                except Exception as exc:
                    logger.exception("Callback %s failed for %r", _name(callback), response)
                    await self._events.emit(RequestFailed(request, exc))
                    return _describe(exc)
                await self._handle_output(output, response)

    async def _schedule(self, request: Request) -> bool:
        veto = await self._events.gate(RequestScheduled(request))
        if veto is not None:
            await self._events.emit(RequestDropped(request, veto.reason))
            return False
        [added] = await self._frontier.add([request])
        if not added:
            await self._events.emit(RequestDropped(request, "duplicate"))
        return added

    async def _handle_output(self, output: object, response: Response) -> None:
        match output:
            case None:
                pass
            case Request():
                await self._schedule(output)
            case Mapping():
                try:
                    self._item_sink(output)  # pyright: ignore[reportUnknownArgumentType]
                except Exception as exc:
                    await self._events.emit(ItemError(output, response, exc))
                    raise
                await self._events.emit(ItemScraped(output, response))
            case _:
                logger.warning("Ignoring unsupported output %r from the Callback for %r", output, response.request)


def run_callbacks(spider: Spider) -> HandleResponse:
    """Return the innermost step of the spider chain, which runs Callbacks.

    Args:
        spider: The Spider whose `parse` handles Responses whose Request names no
            Callback.

    Returns:
        A function that calls the Callback of a Response's Request, with the
        Request's `kwargs`, and yields its outputs. Requests constructed in the
        Callback, including while it is closed, resolve relative URLs against that
        Response.
    """

    def run_callback(response: Response) -> AsyncGenerator[object]:
        callback = response.request.callback or spider.parse
        return resolving_against(response, _outputs(callback, response))

    return run_callback


async def _iterate[T](items: AsyncIterator[T]) -> AsyncGenerator[T]:
    """Iterate `items`, closing it promptly if it is an async generator."""
    if isinstance(items, AsyncGenerator):
        async with aclosing(cast("AsyncGenerator[T]", items)) as generator:
            async for item in generator:
                yield item
    else:
        async for item in items:
            yield item


async def _outputs(callback: Callable[..., object], response: Response) -> AsyncGenerator[object]:
    """Call a Callback and yield its outputs, from an async iterator or an awaitable of a value or list.

    The Callback is called on the first iteration, with the Request's `kwargs`, so an
    exception raised by the call itself surfaces from `anext()` like any other Callback
    exception.
    """
    result = callback(response, **response.request.kwargs)
    if isinstance(result, AsyncIterator):
        async with aclosing(_iterate(cast("AsyncIterator[object]", result))) as outputs:
            async for output in outputs:
                yield output
        return
    value = await result if inspect.isawaitable(result) else result
    if isinstance(value, list):
        for output in cast("list[object]", value):
            yield output
    else:
        yield value


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _name(callback: Callable[..., Any]) -> str:
    return getattr(callback, "__qualname__", repr(callback))
