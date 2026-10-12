from __future__ import annotations

import asyncio
import inspect
import logging
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
from pernix.request import Request, resolve_relative_urls
from pernix.response import expect_response

if TYPE_CHECKING:
    from pernix.events import EventBus
    from pernix.memory_frontier import MemoryFrontier
    from pernix.middleware import Download
    from pernix.response import Response
    from pernix.spider import Spider
    from pernix.spider_middleware import HandleResponse

__all__ = ["Engine", "ItemSink", "run_callbacks"]

logger = logging.getLogger(__name__)

type ItemSink = Callable[[Mapping[str, Any]], None]
"""Receives each Item a Callback produces."""


class Engine:
    """Runs a fixed number of workers that fetch Requests and run their Callbacks.

    Each Request is fetched through the download chain, and its Response handled
    through the spider chain, which ends in the Callback. The Run ends once `start()`
    is exhausted, the Frontier is empty, and nothing is in flight. Exceptions raised
    while fetching or inside a Callback are logged, emitted as `RequestFailed`, and do
    not stop the Run; `IgnoreRequest` raised while fetching is emitted as
    `RequestIgnored` instead. An exception raised while storing an Item ends the Run.

    Every Request passes the `RequestScheduled` gate before entering the Frontier; a
    vetoed or duplicate Request is emitted as `RequestDropped`. Each fetched Response
    is emitted as `ResponseReceived`, each stored Item as `ItemScraped`, and an Item
    that fails to be stored as `ItemError`. `SpiderIdle` is emitted once the Frontier
    has drained, before the workers stop.

    While the spider chain runs, Requests constructed in it resolve relative URLs
    against the Response being handled.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        frontier: MemoryFrontier,
        download: Download,
        handle: HandleResponse,
        item_sink: ItemSink,
        events: EventBus,
        concurrency: int,
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

        Raises:
            ValueError: `concurrency` is less than 1.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self._spider = spider
        self._frontier = frontier
        self._download = download
        self._handle = handle
        self._item_sink = item_sink
        self._events = events
        self._concurrency = concurrency

    async def run(self) -> None:
        """Run until every Request has been fetched and its Callback has finished.

        Raises:
            ExceptionGroup: Storing an Item failed; it holds the original exception.
        """
        async with asyncio.TaskGroup() as tasks:
            for _ in range(self._concurrency):
                tasks.create_task(self._work())
            await self._add_start_requests()
            await self._frontier.join()
            await self._events.emit(SpiderIdle(self._spider))
            self._frontier.shutdown()

    async def _add_start_requests(self) -> None:
        try:
            async with aclosing(_iterate(self._spider.start())) as requests:
                async for request in requests:
                    await self._schedule(request)
        except Exception:
            logger.exception("start() of spider %r failed", self._spider.name)

    async def _work(self) -> None:
        while True:
            try:
                request = await self._frontier.get()
            except asyncio.QueueShutDown:
                return
            try:
                await self._process(request)
            finally:
                self._frontier.done()

    async def _process(self, request: Request) -> None:
        response = await self._fetch(request)
        if response is not None:
            await self._events.emit(ResponseReceived(response))
            await self._handle_response(request, response)

    async def _fetch(self, request: Request) -> Response | None:
        """Fetch through the download chain; `None` if the Request was ignored or failed."""
        try:
            return expect_response(await self._download(request), f"the download chain for {request!r}")
        except IgnoreRequest as exc:
            logger.debug("Ignored %r: %s", request, exc.reason)
            await self._events.emit(RequestIgnored(request, exc.reason))
        except Exception as exc:
            logger.exception("Fetching %r failed", request)
            await self._events.emit(RequestFailed(request, exc))
        return None

    async def _handle_response(self, request: Request, response: Response) -> None:
        """Run the spider chain for a Response and dispatch its outputs."""
        callback = request.callback or self._spider.parse
        async with aclosing(_iterate(self._handle(response))) as outputs:
            while True:
                # Only advancing the Callback is isolated: a failure to store its
                # output is not the Callback's fault and must not be swallowed.
                try:
                    with resolve_relative_urls(response.urljoin):
                        output = await anext(outputs)
                except StopAsyncIteration:
                    return
                except Exception as exc:
                    logger.exception("Callback %s failed for %r", _name(callback), response)
                    await self._events.emit(RequestFailed(request, exc))
                    return
                await self._handle_output(output, response)

    async def _schedule(self, request: Request) -> None:
        veto = await self._events.gate(RequestScheduled(request))
        if veto is not None:
            await self._events.emit(RequestDropped(request, veto.reason))
        elif not self._frontier.add(request):
            await self._events.emit(RequestDropped(request, "duplicate"))

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
        Callback resolve relative URLs against that Response.
    """

    async def run_callback(response: Response) -> AsyncGenerator[object]:
        callback = response.request.callback or spider.parse
        async with aclosing(_outputs(callback, response)) as outputs:
            while True:
                try:
                    with resolve_relative_urls(response.urljoin):
                        output = await anext(outputs)
                except StopAsyncIteration:
                    return
                yield output

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


def _name(callback: Callable[..., Any]) -> str:
    return getattr(callback, "__qualname__", repr(callback))
