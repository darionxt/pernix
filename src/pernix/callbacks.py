"""Running Callbacks and errbacks, and collecting what they produce."""

from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from pernix.middlewares import resolving_against
from pernix.urls import join_url

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.middlewares import HandleResponse
    from pernix.request import Request
    from pernix.response import Response
    from pernix.spider import Spider

__all__ = ["iterate_and_close", "run_callbacks", "run_errback"]


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
        return resolving_against(response.urljoin, _outputs(lambda: callback(response, **response.request.kwargs)))

    return run_callback


def run_errback(request: Request, error: Exception) -> AsyncGenerator[object]:
    """Call the errback of a failed Request as `errback(request, error)` and yield its outputs.

    Requests constructed in the errback resolve relative URLs against the failed
    Request's URL.

    Args:
        request: The failed Request; it must have an errback.
        error: Why it failed.

    Returns:
        The errback's outputs, as from a Callback.

    Raises:
        ValueError: The Request has no errback.
    """
    errback = request.errback
    if errback is None:
        raise ValueError(f"{request!r} has no errback")
    return resolving_against(functools.partial(join_url, request.url), _outputs(lambda: errback(request, error)))


async def iterate_and_close[T](items: AsyncIterator[T]) -> AsyncGenerator[T]:
    """Iterate `items`, closing it promptly if it is an async generator.

    Args:
        items: Outputs, such as those of `Spider.start()`.

    Yields:
        The same items.
    """
    if isinstance(items, AsyncGenerator):
        async with aclosing(cast("AsyncGenerator[T]", items)) as generator:
            async for item in generator:
                yield item
    else:
        async for item in items:
            yield item


async def _outputs(call: Callable[[], Any]) -> AsyncGenerator[object]:
    """Call a Callback and yield its outputs, from an async iterator or an awaitable of a value or list.

    The call happens on the first iteration, so an exception raised by the call
    itself surfaces from `anext()` like any other exception of the Callback.
    """
    result = call()
    if isinstance(result, AsyncIterator):
        async with aclosing(iterate_and_close(cast("AsyncIterator[object]", result))) as outputs:
            async for output in outputs:
                yield output
        return
    value = await result if inspect.isawaitable(result) else result
    if isinstance(value, list):
        for output in cast("list[object]", value):
            yield output
    else:
        yield value
