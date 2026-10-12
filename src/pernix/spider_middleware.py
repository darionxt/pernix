"""Spider Middleware: onion layers around Callbacks (ADR 0002).

Each layer is an async generator that receives the Response and `call_next`, which
runs the layers inside it and the Callback and yields their outputs. A layer may
pass on a different Response, filter, add, or change outputs, and catch the
exceptions the Callback raises while it iterates `call_next`.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from pernix.errors import ComponentError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pernix.response import Response

__all__ = ["HandleResponse", "SpiderMiddleware", "build_spider_chain", "spider_middleware"]

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


def _link(layer: Callable[[Response, HandleResponse], AsyncIterator[Any]], call_next: HandleResponse) -> HandleResponse:
    def handle(response: Response) -> AsyncIterator[Any]:
        return layer(response, call_next)

    return handle


def _is_async_generator(component: object) -> bool:
    """Whether `component` is an async generator function, or an object whose `__call__` is one."""
    return inspect.isasyncgenfunction(component) or inspect.isasyncgenfunction(type(component).__call__)
