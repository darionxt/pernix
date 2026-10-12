"""Download Middleware: onion layers around fetching (ADR 0002).

Each layer receives the Request and `call_next`, which fetches through the layers
inside it and the Fetcher. A layer may change the Request, return without calling
`call_next` (a cache), call it more than once (a retry), and change or replace the
Response. Exceptions travel back out through the same layers.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import cast

from pernix.errors import ComponentError
from pernix.request import Request
from pernix.response import Response, expect_response

__all__ = ["Download", "Middleware", "NodeMiddleware", "build_download_chain", "middleware"]

type Download = Callable[[Request], Awaitable[Response]]
"""Fetches a Request through the rest of the chain: what a Middleware's `call_next` is."""


class Middleware(ABC):
    """Base class for download Middleware.

    Register Middleware with the `middlewares` setting as `{middleware: order}`; a
    lower order is further out. Like Extensions, Middleware classes get services
    injected, have `events.on()` methods subscribed, and are opened and closed with
    the Run.

    Example:
        >>> class Sign(Middleware):
        ...     async def __call__(self, request, call_next):
        ...         response = await call_next(request.replace(headers=signed(request)))
        ...         if response.status == 401:
        ...             ...
        ...         return response
    """

    @abstractmethod
    async def __call__(self, request: Request, call_next: Download) -> Response:
        """Fetch a Request through this layer.

        Args:
            request: The Request coming from the outer layers.
            call_next: Fetches a Request through the inner layers and the Fetcher.

        Returns:
            The Response for the outer layers.
        """


def middleware(function: Callable[[Request, Download], Awaitable[Response]]) -> Middleware:
    """Turn an `async def f(request, call_next)` function into a download Middleware.

    Args:
        function: The coroutine function.

    Returns:
        A Middleware that calls it; register it like any other.

    Raises:
        TypeError: `function` is not a coroutine function.

    Example:
        >>> @middleware
        ... async def timing(request, call_next):
        ...     started = time.monotonic()
        ...     try:
        ...         return await call_next(request)
        ...     finally:
        ...         log(request, time.monotonic() - started)
    """
    if not inspect.iscoroutinefunction(function):
        raise TypeError(f"@middleware needs an 'async def' function, got {function!r}")
    return _FunctionMiddleware(function)


class _FunctionMiddleware(Middleware):
    def __init__(self, function: Callable[[Request, Download], Awaitable[Response]]) -> None:
        self.__wrapped__ = function

    async def __call__(self, request: Request, call_next: Download) -> Response:
        return await self.__wrapped__(request, call_next)

    def __repr__(self) -> str:
        return f"<middleware {self.__wrapped__.__module__}.{self.__wrapped__.__qualname__}>"


class NodeMiddleware(Middleware):
    """Adapts Middleware written as hooks to the onion.

    Override any of the hooks, as plain or `async` methods:

    - `process_request(request)`: return `None` to continue inward, or a Response to
      skip the inner layers; the Response still passes through `process_response`.
    - `process_response(request, response)`: return the Response, or a replacement.
    - `process_exception(request, exception)`: called for exceptions from the inner
      layers; return `None` to let the exception propagate, or a Response to use
      instead, which passes through `process_response`.

    A hook may also return a Request: it is then fetched in place of the original,
    starting again at this layer. It does not go back to the Frontier.
    """

    def process_request(self, request: Request) -> Request | Response | Awaitable[Request | Response | None] | None:
        """Inspect or answer a Request before the inner layers see it.

        Args:
            request: The Request.

        Returns:
            `None` to continue, a Response to answer it, or a Request to fetch instead.
        """
        return None

    def process_response(
        self, request: Request, response: Response
    ) -> Request | Response | Awaitable[Request | Response]:
        """Inspect or replace a Response on its way out.

        Args:
            request: The Request the Response answers.
            response: The Response.

        Returns:
            The Response to pass on, or a Request to fetch instead.
        """
        return response

    def process_exception(
        self, request: Request, exception: Exception
    ) -> Request | Response | Awaitable[Request | Response | None] | None:
        """Recover from an exception raised by the inner layers.

        Args:
            request: The Request being fetched.
            exception: The exception.

        Returns:
            `None` to let the exception propagate, a Response to use instead, or a
            Request to fetch instead.
        """
        return None

    async def __call__(self, request: Request, call_next: Download) -> Response:
        """Fetch a Request through the hooks; see the class description.

        Args:
            request: The Request coming from the outer layers.
            call_next: Fetches a Request through the inner layers and the Fetcher.

        Returns:
            The Response for the outer layers.

        Raises:
            TypeError: A hook returned something other than what it may return.
        """
        while True:
            answer = await self._answer(request, call_next)
            if isinstance(answer, Request):
                request = answer
                continue
            result = await _resolve(self.process_response(request, answer))
            if isinstance(result, Request):
                request = result
                continue
            return expect_response(result, f"process_response of {self!r}")

    async def _answer(self, request: Request, call_next: Download) -> Request | Response:
        """Answer a Request from `process_request`, or from the inner layers and `process_exception`."""
        answer = await _resolve(self.process_request(request))
        if answer is None:
            try:
                return await call_next(request)
            except Exception as exc:
                answer = await _resolve(self.process_exception(request, exc))
                if answer is None:
                    raise
        return answer if isinstance(answer, Request) else expect_response(answer, f"a hook of {self!r}")


def build_download_chain(middlewares: Sequence[object], fetch: Download) -> Download:
    """Wrap a fetch function in download Middleware.

    Args:
        middlewares: Built Middleware, outermost first: each must be an async callable
            taking `(request, call_next)`.
        fetch: The innermost step, which fetches with a Fetcher.

    Returns:
        A function that fetches a Request through every layer.

    Raises:
        ComponentError: A Middleware is not an async callable.
    """
    download = fetch
    for layer in reversed(middlewares):
        if not _is_async_callable(layer):
            raise ComponentError(
                f"download Middleware {layer!r} is not an async callable; define "
                "'async def __call__(self, request, call_next)' or decorate a function with @middleware"
            )
        download = _link(cast("Callable[[Request, Download], Awaitable[Response]]", layer), download)
    return download


def _link(layer: Callable[[Request, Download], Awaitable[Response]], call_next: Download) -> Download:
    async def download(request: Request) -> Response:
        return await layer(request, call_next)

    return download


def _is_async_callable(component: object) -> bool:
    """Whether `component` is an async function, or an object with `async def __call__`."""
    return inspect.iscoroutinefunction(component) or inspect.iscoroutinefunction(type(component).__call__)


async def _resolve[T](result: T | Awaitable[T]) -> T:
    if inspect.isawaitable(result):
        return await cast("Awaitable[T]", result)
    return cast("T", result)
