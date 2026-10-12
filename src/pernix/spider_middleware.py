"""Spider Middleware: onion layers around Callbacks (ADR 0002).

Each layer is an async generator that receives the Response and `call_next`, which
runs the layers inside it and the Callback and yields their outputs. A layer may
pass on a different Response, filter, add, or change outputs, and catch the
exceptions the Callback raises while it iterates `call_next`. Requests a layer
constructs resolve relative URLs against the Response that layer received.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, aclosing
from typing import TYPE_CHECKING, Any, cast

from pernix.errors import ComponentError
from pernix.request import resolve_relative_urls

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pernix.response import Response

__all__ = ["HandleResponse", "SpiderMiddleware", "build_spider_chain", "resolving_against", "spider_middleware"]

type HandleResponse = Callable[[Response], AsyncIterator[Any]]
"""Runs the rest of the chain for a Response and yields the outputs: a spider Middleware's `call_next`."""


class SpiderMiddleware(ABC):
    """Base class for spider Middleware.

    Register spider Middleware with the `spider_middlewares` setting as
    `{middleware: order}`; a lower order is further out. Like Extensions, they get
    services injected, have `events.on()` methods subscribed, and are opened and
    closed with the Run.

    Example:
        >>> class DropEmpty(SpiderMiddleware):
        ...     async def __call__(self, response, call_next):
        ...         async for output in call_next(response):
        ...             if output:
        ...                 yield output
    """

    @abstractmethod
    def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
        """Handle a Response through this layer; implement it as an async generator.

        Args:
            response: The Response coming from the outer layers.
            call_next: Runs the inner layers and the Callback, yielding their outputs.

        Returns:
            The outputs for the outer layers: Requests and Items.
        """


def spider_middleware(function: Callable[[Response, HandleResponse], AsyncIterator[Any]]) -> SpiderMiddleware:
    """Turn an async generator function `f(response, call_next)` into a spider Middleware.

    Args:
        function: The async generator function.

    Returns:
        A spider Middleware that calls it; register it like any other.

    Raises:
        TypeError: `function` is not an async generator function.
    """
    if not inspect.isasyncgenfunction(function):
        raise TypeError(f"@spider_middleware needs an async generator function, got {function!r}")
    return _FunctionSpiderMiddleware(function)


class _FunctionSpiderMiddleware(SpiderMiddleware):
    def __init__(self, function: Callable[[Response, HandleResponse], AsyncIterator[Any]]) -> None:
        self.__wrapped__ = function

    async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
        # spider_middleware() checked that the function is an async generator function.
        outputs = cast("AsyncGenerator[Any]", self.__wrapped__(response, call_next))
        async with aclosing(outputs):
            async for output in outputs:
                yield output

    def __repr__(self) -> str:
        return f"<spider_middleware {self.__wrapped__.__module__}.{self.__wrapped__.__qualname__}>"


def build_spider_chain(middlewares: Sequence[object], handle: HandleResponse) -> HandleResponse:
    """Wrap the Callback runner in spider Middleware.

    Args:
        middlewares: Built spider Middleware, outermost first: each must be an async
            generator function, or an object whose `__call__` is one, taking
            `(response, call_next)`.
        handle: The innermost step, which runs the Callback.

    Returns:
        A function that handles a Response through every layer.

    Raises:
        ComponentError: A spider Middleware is not an async generator.
    """
    for layer in reversed(middlewares):
        if not _is_async_generator(layer):
            raise ComponentError(
                f"spider Middleware {layer!r} is not an async generator; define "
                "'async def __call__(self, response, call_next)' that yields, or decorate a function "
                "with @spider_middleware"
            )
        handle = _link(cast("Callable[[Response, HandleResponse], AsyncIterator[Any]]", layer), handle)
    return handle


def resolving_against(response: Response, outputs: AsyncIterator[Any]) -> AsyncGenerator[Any]:
    """Iterate outputs with relative URLs resolving against a Response.

    Each step, and the closing of `outputs` once iteration ends or is abandoned, runs
    with Requests constructed in it resolving relative URLs against `response`.

    Args:
        response: The Response the code producing `outputs` handles.
        outputs: The outputs of a spider Middleware layer or a Callback.

    Returns:
        An async generator of the same outputs.
    """

    async def resolving() -> AsyncGenerator[Any]:
        try:
            while True:
                with resolve_relative_urls(response.urljoin):
                    try:
                        output = await anext(outputs)
                    except StopAsyncIteration:
                        return
                yield output
        finally:
            with resolve_relative_urls(response.urljoin):
                await _aclose(outputs)

    return resolving()


def _link(layer: Callable[[Response, HandleResponse], AsyncIterator[Any]], call_next: HandleResponse) -> HandleResponse:
    """Run `layer` with `call_next` as its inner chain.

    The layer runs against the Response it received (see `resolving_against()`).
    When it ends, however it ends, the inner iterators it opened through `call_next`
    are closed too, so their cleanup runs before the chain is done rather than
    whenever they are garbage collected.
    """

    async def handle(response: Response) -> AsyncGenerator[Any]:
        opened: list[AsyncIterator[Any]] = []

        def tracked_call_next(inner_response: Response) -> AsyncIterator[Any]:
            inner = call_next(inner_response)
            opened.append(inner)
            return inner

        outputs = resolving_against(response, layer(response, tracked_call_next))
        try:
            async for output in outputs:
                yield output
        finally:
            async with AsyncExitStack() as stack:  # closes the layer first, then the inner iterators
                for iterator in (*opened, outputs):
                    stack.push_async_callback(_aclose, iterator)

    return handle


async def _aclose(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if aclose is not None:
        await aclose()


def _is_async_generator(component: object) -> bool:
    """Whether `component` is an async generator function, or an object whose `__call__` is one."""
    return inspect.isasyncgenfunction(component) or inspect.isasyncgenfunction(type(component).__call__)
