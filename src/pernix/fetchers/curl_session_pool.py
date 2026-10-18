from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from curl_cffi import Curl, CurlInfo, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR, CURL_WRITEFUNC_PAUSE, CURLPAUSE_CONT
from curl_cffi.requests import AsyncSession

from pernix.errors import ResponseTooLarge
from pernix.fetchers.redirects import REDIRECT_STATUSES

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

__all__ = ["BUFFER_SIZE", "CurlSession", "CurlSessionPool", "Transfer"]

logger = logging.getLogger(__name__)

BUFFER_SIZE = 1024 * 1024
"""Bytes of body a transfer holds, delivered but not read yet, before curl stops receiving."""


@dataclass(slots=True)
class Transfer:
    """The curl handle of one request, and how much body it delivered.

    Once `BUFFER_SIZE` bytes wait to be read, curl stops receiving; it resumes once
    half of them have been read, as `read()` notes.

    Attributes:
        curl: The handle.
        limit: Bytes of body it may deliver; `None` for no limit, as for the body of
            a redirect, which is dropped.
        received: Bytes of body delivered so far, decoded.
        buffered: Bytes of body delivered but not read yet.
        exceeded: Whether the limit stopped it.
        paused: Whether curl stopped receiving until more is read.
        returned: Whether the handle went back to the session's pool.
    """

    curl: Curl
    limit: int | None
    received: int = 0
    buffered: int = 0
    exceeded: bool = False
    paused: bool = False
    returned: bool = False

    def read(self, size: int) -> None:
        """Note that bytes of body were read, resuming curl once few enough wait.

        Args:
            size: The bytes read.

        Raises:
            curl_cffi.CurlError: curl failed to resume.
        """
        self.buffered -= size
        if self.paused and self.buffered <= BUFFER_SIZE // 2 and not self.returned:
            self.paused = False
            self.curl.pause(CURLPAUSE_CONT)  # curl delivers the chunk it kept first


@dataclass(slots=True)
class _Start:
    url: str
    limit: int | None
    cookie: str | None
    on_headers: HeadersCallback | None
    transfer: Transfer | None = None


type HeadersCallback = Callable[[str, list[str]], object]
"""Called with the URL a transfer requested and the `Set-Cookie` values of its response."""

_starting: ContextVar[_Start | None] = ContextVar("pernix_curl_start", default=None)
"""The request the current task is starting with `CurlSession.start()`, if any."""


class CurlSession(AsyncSession):
    """A curl-cffi session that bounds each streamed body as curl receives it, and stops abandoned transfers.

    curl-cffi queues a streamed body without bound, and runs the transfer in a task of
    its own, which goes on when the request waiting for the headers is cancelled.
    `start()` therefore notes the handle a request takes, through the `pop_curl()` and
    `push_curl()` hooks of curl-cffi's handle pool. The handle's write callback counts
    the body, failing the transfer past the limit, and pauses curl while
    `BUFFER_SIZE` bytes wait to be read; the handle is removed from curl when the
    request is cancelled. The header callback reports the `Set-Cookie` headers of the
    response as they arrive, and lifts the limit for the body of a redirect. The
    `Cookie` header a request is started with is set once curl-cffi has cleared the
    cookies of curl's own cookie engine, which then adds none.
    """

    def __init__(self, **options: Any) -> None:
        """Create the session.

        Args:
            **options: The options of `curl_cffi.requests.AsyncSession`.
        """
        super().__init__(**options)
        self._transfers: dict[Curl, Transfer] = {}

    async def start(
        self,
        method: HttpMethod,
        url: str,
        *,
        body_limit: int | None,
        cookie: str | None = None,
        on_headers: HeadersCallback | None = None,
        **options: Any,
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Start a streamed transfer and wait for its headers.

        Args:
            method: The HTTP method.
            url: The URL.
            body_limit: Bytes of body the transfer may deliver; `None` for no limit.
                The body of a redirect, which is dropped, is not limited.
            cookie: The cookies to send, as the value of a `Cookie` header, which
                curl sends as one header over HTTP/1.1 and as a `cookie` field each
                over HTTP/2; `None` for none.
            on_headers: Called at the end of the headers of the response, unless it
                is informational, with the URL it answered and its `Set-Cookie`
                values; an error it raises is logged.
            **options: The options of `AsyncSession.request()`, but `stream` and
                `cookies`.

        Returns:
            The response, its body still to read, and the transfer.

        Raises:
            curl_cffi.CurlError: The transfer failed before the headers arrived.
            pernix.errors.ResponseTooLarge: The limit stopped the transfer before the
                headers came back.
        """
        start = _Start(url, body_limit, cookie, on_headers)
        token = _starting.set(start)  # this request's own, not one a parent task started
        try:
            response = await self.request(method, url, stream=True, **options)
        except asyncio.CancelledError:
            transfer = start.transfer
            if transfer is not None and not transfer.returned:
                self.acurl.remove_handle(transfer.curl)  # curl-cffi then ends the transfer
            raise
        except Exception as exc:
            if start.transfer is not None and start.transfer.exceeded:
                raise ResponseTooLarge(f"the body of {url} exceeds {body_limit} bytes") from exc
            raise
        finally:
            _starting.reset(token)
        if start.transfer is None:
            raise RuntimeError("curl-cffi started a transfer without taking a handle")
        return response, start.transfer

    async def pop_curl(self) -> Curl:
        """Take a handle for a transfer, noting it for the request being started."""
        curl = await super().pop_curl()
        start = _starting.get()
        if start is not None and start.transfer is None:
            transfer = start.transfer = Transfer(curl, start.limit)
            self._transfers[curl] = transfer
            watched = _watched(curl.setopt, transfer, start)
            setattr(curl, "setopt", watched)  # noqa: B010  # before curl-cffi sets it up
        return curl

    def push_curl(self, curl: Curl | None) -> None:
        """Return a handle at the end of its transfer."""
        if curl is not None and (transfer := self._transfers.pop(curl, None)) is not None:
            transfer.returned = True
            vars(curl).pop("setopt", None)
        super().push_curl(curl)


def _watched(setopt: Callable[[CurlOpt, Any], int], transfer: Transfer, start: _Start) -> Callable[[CurlOpt, Any], int]:
    """Wrap a handle's `setopt()` so that the callbacks it installs meter the body and report cookies.

    It also sets the `Cookie` header of the request once curl-cffi has cleared the
    cookies the handle held.
    """

    def watched_setopt(option: CurlOpt, value: Any) -> int:
        if option == CurlOpt.WRITEFUNCTION and callable(value):
            write: Callable[[bytes], Any] = value

            def metered(chunk: bytes) -> Any:
                if transfer.buffered >= BUFFER_SIZE:
                    transfer.paused = True
                    return CURL_WRITEFUNC_PAUSE  # curl keeps the chunk, and delivers it again once resumed
                transfer.received += len(chunk)
                if transfer.limit is not None and transfer.received > transfer.limit:
                    transfer.exceeded = True
                    return CURL_WRITEFUNC_ERROR  # curl stops receiving at once
                transfer.buffered += len(chunk)
                return write(chunk)

            value = metered
        elif option == CurlOpt.HEADERDATA:
            option, value = CurlOpt.HEADERFUNCTION, _HeaderWatch(value, start.on_headers, transfer)
        elif option == CurlOpt.COOKIELIST and value == "ALL" and start.cookie:
            result = setopt(option, value)
            setopt(CurlOpt.COOKIE, start.cookie)
            return result
        return setopt(option, value)

    return watched_setopt


class _HeaderWatch:
    """Writes the response headers of one transfer to curl-cffi's buffer, reporting their cookies.

    At the end of the headers of the response, it reports the URL curl requested for
    it and its `Set-Cookie` values, decoded as UTF-8 unless their bytes are not UTF-8,
    then as Latin-1. curl unfolds folded lines itself; one that gets
    through still continues the header before it. Informational responses and 407s,
    which answer for a proxy, are not reported, nor are headers after the body,
    trailers. The headers of a redirect, a redirect status with a `Location`, lift
    the limit of the transfer before curl delivers its body.
    """

    def __init__(self, buffer: Any, on_headers: HeadersCallback | None, transfer: Transfer) -> None:
        self._buffer = buffer
        self._on_headers = on_headers
        self._transfer = transfer
        self._status = 0
        self._set_cookies: list[str] = []
        self._in_set_cookie = False
        self._located = False

    def __call__(self, line: bytes) -> int:
        text = line.decode("latin-1").strip()
        if text.startswith("HTTP/"):
            code = text.split(maxsplit=2)[1:2]
            self._status = int(code[0]) if code and code[0].isdigit() else 0
            self._set_cookies, self._in_set_cookie, self._located = [], False, False
        elif not text:
            self._end()
        elif self._status < 200:  # an informational response, or trailers
            pass
        elif line[:1] in (b" ", b"\t"):  # a folded line, continuing the header before it
            if self._in_set_cookie:
                self._set_cookies[-1] += f" {text}"
        else:
            name, _, value = text.partition(":")
            name, value = name.strip().lower(), value.strip()
            self._in_set_cookie = name == "set-cookie"
            if self._in_set_cookie:
                self._set_cookies.append(value)
            elif name == "location" and value:
                self._located = True
        self._buffer.write(line)
        return len(line)

    def _url(self) -> str:
        """The URL curl requested for the response it is receiving, with its path as curl normalized it."""
        effective = cast("bytes", self._transfer.curl.getinfo(CurlInfo.EFFECTIVE_URL))
        return effective.decode("utf-8", "replace")  # a host left unreadable takes no cookies

    def _end(self) -> None:
        """End the headers of a response, reporting its cookies, and lifting the limit for the body of a redirect."""
        if self._status in REDIRECT_STATUSES and self._located:
            self._transfer.limit = None
        if self._status >= 200 and self._status != 407 and self._on_headers is not None and self._set_cookies:
            url = self._url()
            try:
                self._on_headers(url, [_text(value) for value in self._set_cookies])
            except Exception:
                logger.exception("Handling the Set-Cookie headers of %s failed", url)
        if self._status >= 200:
            self._status = 0  # what follows the body is no response


def _text(value: str) -> str:
    """A header value read as Latin-1, decoded as UTF-8 instead, unless its bytes are not UTF-8.

    Cookies are sent encoded as UTF-8, so those received as UTF-8 go back byte for byte.
    """
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeDecodeError:
        return value


@dataclass(slots=True)
class _Entry:
    session: CurlSession
    users: int = 0
    last_used: float = field(default_factory=time.monotonic)


class CurlSessionPool:
    """Keeps one curl session per Profile and owner, so connections are reused but never shared between them.

    The owner is the Session Requests are sent under, if any, so two Sessions never
    share a connection.

    A session is created on first use, inside the running event loop, with curl's own
    retries off, proxies from the environment ignored, `NO_PROXY` included, and HTTP/2
    connections reused.
    Beyond `size` sessions, the least recently used one not in use is closed, and so
    is one unused for `idle` seconds, including the one about to be used, which is
    then replaced; both are checked whenever a session is taken. Retired sessions
    close in tasks of their own, so taking a session never waits for them, and a
    cancellation never leaves one open; `close()` waits for them.
    """

    def __init__(self, *, size: int, idle: float, max_clients: int) -> None:
        """Configure the pool.

        Args:
            size: Sessions kept at most, beyond those in use.
            idle: Seconds a session is kept unused.
            max_clients: Transfers in flight at once per session.
        """
        self._size = size
        self._idle = idle
        self._max_clients = max_clients
        self._entries: dict[tuple[str, str | None], _Entry] = {}  # least recently used first
        self._closing: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def session(self, profile: str, *, owner: str | None = None) -> AsyncGenerator[CurlSession]:
        """Use the session of a Profile and owner, creating it if needed.

        Args:
            profile: The Profile, already checked.
            owner: The id of the Session Requests are sent under; `None` for none.

        Yields:
            The session; it stays open while in use.
        """
        now = time.monotonic()
        key = (profile, owner)
        entry = self._entries.pop(key, None)
        if entry is not None and entry.users == 0 and now - entry.last_used > self._idle:
            self._retire(entry.session)
            entry = None
        if entry is None:
            entry = _Entry(self._new_session(profile))
        self._entries[key] = entry
        entry.users += 1
        for evicted in self._detach_evicted(now):
            self._retire(evicted)
        try:
            yield entry.session
        finally:
            entry.users -= 1
            entry.last_used = time.monotonic()

    async def close(self) -> None:
        """Close every session, and wait for those being retired."""
        entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            self._retire(entry.session)
        if self._closing:
            await asyncio.wait(self._closing)

    def _retire(self, session: CurlSession) -> None:
        """Close a session no longer in the pool, in a task of its own."""
        closing = asyncio.create_task(_close(session))
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)

    def _new_session(self, profile: str) -> CurlSession:
        return CurlSession(
            max_clients=self._max_clients,
            impersonate=profile,
            trust_env=False,
            retry=0,
            # libcurl reads NO_PROXY itself, even for a proxy given explicitly; "" stops that.
            # A proxy's answers to CONNECT are not the headers of a response to report.
            curl_options={CurlOpt.PIPEWAIT: 1, CurlOpt.NOPROXY: "", CurlOpt.SUPPRESS_CONNECT_HEADERS: 1},
        )

    def _detach_evicted(self, now: float) -> list[CurlSession]:
        """Remove the sessions to close: the least recently used beyond `size`, and the idle ones."""
        unused = [key for key, entry in self._entries.items() if entry.users == 0]
        excess = max(0, len(self._entries) - self._size)
        evicted = unused[:excess] + [key for key in unused[excess:] if now - self._entries[key].last_used > self._idle]
        return [self._entries.pop(key).session for key in evicted]


async def _close(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:
        logger.exception("Closing a curl session failed")
