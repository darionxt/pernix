from __future__ import annotations

import inspect
from contextlib import AsyncExitStack, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from pernix.errors import ComponentError, NetworkBlocked, SessionError, SettingsError
from pernix.fetchers.streamed_response import StreamedResponse
from pernix.response import expect_response

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping
    from contextlib import AbstractAsyncContextManager

    from pernix.fetchers.fetcher import Fetcher, StreamingFetcher
    from pernix.request import Request
    from pernix.response import Response
    from pernix.session import Session

__all__ = ["BROWSER_FETCHER", "FetcherDispatch", "Streams", "is_rendered", "sending_as", "streaming_into"]

BROWSER_FETCHER = "browser"
"""The name of the Fetcher that renders Requests with `render` set."""


def is_rendered(request: Request) -> bool:
    """Whether a Request goes to the browser Fetcher, as `FetcherDispatch` picks it.

    Args:
        request: The Request.

    Returns:
        Whether it names the `browser` Fetcher, or names none and needs rendering.
    """
    if request.fetcher is not None:
        return request.fetcher == BROWSER_FETCHER
    return bool(request.render)


class Streams:
    """The transfers one streamed fetch opens through the download chain; only the latest stays open.

    A download Middleware that fetches more than once, as a retry does, makes the
    Fetcher open a transfer each time; opening one closes the one before.

    Attributes:
        max_bytes: Bytes a body may have at most; `None` for the Fetcher's own limit,
            and 0 for no limit.
        latest: The transfer opened last, until closed.
    """

    def __init__(self, max_bytes: int | None) -> None:
        """Prepare for a streamed fetch.

        Args:
            max_bytes: Bytes a body may have at most; `None` for the Fetcher's own
                limit, and 0 for no limit.
        """
        self.max_bytes = max_bytes
        self.latest: StreamedResponse | None = None
        self._exits = AsyncExitStack()

    async def open(self, transfer: AbstractAsyncContextManager[object], source: str) -> StreamedResponse:
        """Close the latest transfer, then open another.

        Args:
            transfer: The Fetcher's `stream()` for the Request.
            source: Names the Fetcher in errors.

        Returns:
            The transfer opened.

        Raises:
            TypeError: The Fetcher gave something other than a StreamedResponse.
        """
        await self.close()
        streamed = await self._exits.enter_async_context(transfer)
        if not isinstance(streamed, StreamedResponse):
            raise TypeError(f"{source} streamed {streamed!r}, which is not a StreamedResponse")
        self.latest = streamed
        return streamed

    async def close(self) -> None:
        """Close the latest transfer, if any."""
        self.latest = None
        exits, self._exits = self._exits, AsyncExitStack()
        await exits.aclose()


_streams: ContextVar[Streams | None] = ContextVar("pernix_streams", default=None)
"""Where the download chain the current task runs opens streamed transfers, if it streams."""


@contextmanager
def streaming_into(streams: Streams | None) -> Generator[None]:
    """Make the download chain called inside the block stream into `streams`, or fetch whole Responses.

    Args:
        streams: Where to open the transfers; `None` to fetch whole Responses, as a
            fetch nested in a streamed one does.

    Yields:
        Nothing; the block runs with `streams` in effect.
    """
    token = _streams.set(streams)
    try:
        yield
    finally:
        _streams.reset(token)


_session: ContextVar[Session | None] = ContextVar("pernix_session", default=None)
"""The Session the download chain the current task runs sends its Request under, if any."""


@contextmanager
def sending_as(session: Session | None) -> Generator[None]:
    """Make the download chain called inside the block send its Request under a Session, or under none.

    Args:
        session: The Session, which the Fetcher gets as `session=`; `None` for none.

    Yields:
        Nothing; the block runs with the Session in effect.
    """
    token = _session.set(session)
    try:
        yield
    finally:
        _session.reset(token)


class FetcherDispatch:
    """The innermost step of the download chain: fetches each Request with its Fetcher.

    The Fetcher is the one named by `request.fetcher`; otherwise the `browser`
    Fetcher if the Request needs rendering; otherwise the one whose `schemes` include
    the scheme of the URL. The Response records the name it was registered under.

    Inside `streaming_into()`, a Fetcher with `stream()` opens its transfer in the
    given `Streams`, and the Response it gives, still without its body, is marked
    `streamed`. Other Fetchers fetch the whole Response. Inside `sending_as()`, the
    Fetcher gets the Session as `session=`; one without that parameter refuses the
    Request.

    With `public_only`, as `network.public_only` sets it, Requests for the `browser`
    Fetcher are refused: the browser resolves hosts itself, so their addresses
    cannot be checked.
    """

    def __init__(self, fetchers: Mapping[str, object], *, public_only: bool = False) -> None:
        """Check the Fetchers and index them by name and by URL scheme.

        Args:
            fetchers: The built Fetchers, by registered name.
            public_only: Whether only hosts on public addresses may be reached.

        Raises:
            ComponentError: A component has no `async def fetch(self, request)`.
            SettingsError: Two Fetchers claim the same URL scheme.
        """
        for name, fetcher in fetchers.items():
            if not inspect.iscoroutinefunction(getattr(fetcher, "fetch", None)):
                raise ComponentError(f"the Fetcher {name!r}, {fetcher!r}, has no 'async def fetch(self, request)'")
        self._fetchers = cast("dict[str, Fetcher]", dict(fetchers))
        self._public_only = public_only
        self._by_scheme: dict[str, str] = {}
        problems: list[str] = []
        for name, fetcher in self._fetchers.items():
            for scheme in sorted(getattr(fetcher, "schemes", frozenset[str]())):
                other = self._by_scheme.setdefault(scheme, name)
                if other != name:
                    problems.append(
                        f"fetchers: {other!r} and {name!r} both fetch {scheme!r} URLs; register a replacement "
                        f"under the name {other!r}, or disable one of them"
                    )
        if problems:
            raise SettingsError(problems)

    async def __call__(self, request: Request) -> Response:
        """Fetch a Request with the Fetcher it needs.

        Args:
            request: The Request.

        Returns:
            The Response, with `fetcher` set to the Fetcher's registered name.

        Raises:
            LookupError: No registered Fetcher fits the Request.
            TypeError: The Fetcher returned something other than a Response, or
                streamed something other than a StreamedResponse.
            pernix.errors.SessionError: The Request is sent under a Session, which
                the Fetcher cannot take.
            pernix.errors.NetworkBlocked: The Request is for the `browser` Fetcher
                while only hosts on public addresses may be reached.
        """
        name = self._select(request)
        if self._public_only and name == BROWSER_FETCHER:
            raise NetworkBlocked(
                f"network.public_only refused {request.url}: the browser resolves hosts itself, so the addresses "
                "it would reach cannot be checked; fetch it over HTTP, or turn network.public_only off to render it"
            )
        fetcher = self._fetchers[name]
        session = _session.get()
        options = {} if session is None else {"session": session}
        streams = _streams.get()
        streaming = streams is not None and callable(getattr(fetcher, "stream", None))
        method: Callable[..., object] = getattr(fetcher, "stream" if streaming else "fetch")
        if session is not None and not _takes_session(method):
            raise SessionError(f"the Fetcher {name!r} cannot send {request!r} under a Session")
        if streams is not None and streaming:
            transfer = cast("StreamingFetcher", fetcher).stream(request, max_bytes=streams.max_bytes, **options)
            streamed = await streams.open(transfer, f"the Fetcher {name!r}")
            response = expect_response(streamed.response, f"the Fetcher {name!r}")
            if not response.streamed:
                response = response.replace(streamed=True)
        else:
            response = expect_response(await fetcher.fetch(request, **options), f"the Fetcher {name!r}")
        return response if response.fetcher == name else response.replace(fetcher=name)

    def _select(self, request: Request) -> str:
        if request.fetcher is not None:
            if request.fetcher not in self._fetchers:
                raise LookupError(f"{request!r} names the Fetcher {request.fetcher!r}, which is not registered")
            return request.fetcher
        if is_rendered(request):
            if BROWSER_FETCHER not in self._fetchers:
                raise LookupError(f"{request!r} needs rendering, but the {BROWSER_FETCHER!r} Fetcher is not registered")
            return BROWSER_FETCHER
        scheme = urlsplit(request.url).scheme
        try:
            return self._by_scheme[scheme]
        except KeyError:
            raise LookupError(
                f"no registered Fetcher fetches {scheme!r} URLs such as {request!r}; register one with the "
                "fetchers setting, or name one with Request(fetcher=...)"
            ) from None


def _takes_session(method: Callable[..., object]) -> bool:
    """Whether a Fetcher's method takes the `session` keyword."""
    parameters = inspect.signature(method).parameters.values()
    return any(parameter.name == "session" or parameter.kind is parameter.VAR_KEYWORD for parameter in parameters)
