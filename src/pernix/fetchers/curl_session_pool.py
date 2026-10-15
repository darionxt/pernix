from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

from curl_cffi import Curl, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR, CURL_WRITEFUNC_PAUSE, CURLPAUSE_CONT
from curl_cffi.requests import AsyncSession

from pernix.errors import NetworkBlocked, ResponseTooLarge

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

    from pernix.fetchers.network_guard import NetworkGuard

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
        limit: Bytes of body it may deliver; `None` for no limit.
        received: Bytes of body delivered so far, decoded.
        buffered: Bytes of body delivered but not read yet.
        exceeded: Whether the limit stopped it.
        paused: Whether curl stopped receiving until more is read.
        returned: Whether the handle went back to the session's pool.
        blocked: The redirect the network guard refused, which stopped it.
    """

    curl: Curl
    limit: int | None
    received: int = 0
    buffered: int = 0
    exceeded: bool = False
    paused: bool = False
    returned: bool = False
    blocked: str | None = None

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
    guard: NetworkGuard | None
    transfer: Transfer | None = None


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
    request is cancelled. Under a network guard, the header callback stops a transfer
    before curl follows a redirect the guard refuses.
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
        guard: NetworkGuard | None = None,
        **options: Any,
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Start a streamed transfer and wait for its headers.

        Args:
            method: The HTTP method.
            url: The URL.
            body_limit: Bytes of body the transfer may deliver; `None` for no limit.
            guard: The network guard every redirect must pass, if any.
            **options: The options of `AsyncSession.request()`, but `stream`.

        Returns:
            The response, its body still to read, and the transfer.

        Raises:
            curl_cffi.CurlError: The transfer failed before the headers arrived.
            pernix.errors.ResponseTooLarge: The limit stopped the transfer before the
                headers came back.
            pernix.errors.NetworkBlocked: The guard refused a redirect.
        """
        start = _Start(url, body_limit, guard)
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
            if start.transfer is not None and start.transfer.blocked is not None:
                raise NetworkBlocked(
                    f"the network guard refused the redirect from {url} to {start.transfer.blocked}"
                ) from exc
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
            watched = _watched(curl.setopt, transfer, start.url, start.guard)
            setattr(curl, "setopt", watched)  # noqa: B010  # before curl-cffi sets it up
        return curl

    def push_curl(self, curl: Curl | None) -> None:
        """Return a handle at the end of its transfer."""
        if curl is not None and (transfer := self._transfers.pop(curl, None)) is not None:
            transfer.returned = True
            vars(curl).pop("setopt", None)
        super().push_curl(curl)


def _watched(
    setopt: Callable[[CurlOpt, Any], int], transfer: Transfer, url: str, guard: NetworkGuard | None
) -> Callable[[CurlOpt, Any], int]:
    """Wrap a handle's `setopt()` so that the callbacks it installs meter the body and check redirects."""

    def watched_setopt(option: CurlOpt, value: Any) -> int:
        if option == CurlOpt.WRITEFUNCTION and callable(value):
            write: Callable[[bytes], Any] = value
            limit = transfer.limit

            def metered(chunk: bytes) -> Any:
                if transfer.buffered >= BUFFER_SIZE:
                    transfer.paused = True
                    return CURL_WRITEFUNC_PAUSE  # curl keeps the chunk, and delivers it again once resumed
                transfer.received += len(chunk)
                if limit is not None and transfer.received > limit:
                    transfer.exceeded = True
                    return CURL_WRITEFUNC_ERROR  # curl stops receiving at once
                transfer.buffered += len(chunk)
                return write(chunk)

            value = metered
        elif option == CurlOpt.HEADERDATA and guard is not None:
            # A header function, unlike the buffer's write(), can fail the transfer by its return value.
            option, value = CurlOpt.HEADERFUNCTION, _RedirectCheck(value, url, guard, transfer)
        return setopt(option, value)

    return watched_setopt


class _RedirectCheck:
    """Writes the response headers of one transfer to curl-cffi's buffer, refusing redirects the guard does not admit.

    curl follows a redirect after delivering the headers of the response, so failing
    the header callback on a refused `Location` stops the transfer before curl
    connects to it.
    """

    def __init__(self, buffer: Any, url: str, guard: NetworkGuard, transfer: Transfer) -> None:
        self._buffer = buffer
        self._url = url
        self._guard = guard
        self._transfer = transfer
        self._status = 0

    def __call__(self, line: bytes) -> int:
        text = line.decode("latin-1").strip()
        if text.startswith("HTTP/"):
            code = text.split(maxsplit=2)[1:2]
            self._status = int(code[0]) if code and code[0].isdigit() else 0
        elif 300 <= self._status < 400 and text[:9].lower() == "location:":
            self._url = urljoin(self._url, text[9:].strip())
            try:
                self._guard.check(self._url)
            except NetworkBlocked:
                self._transfer.blocked = self._url
                return CURL_WRITEFUNC_ERROR  # curl stops the transfer
        self._buffer.write(line)
        return len(line)


@dataclass(slots=True)
class _Entry:
    session: CurlSession
    users: int = 0
    last_used: float = field(default_factory=time.monotonic)


class CurlSessionPool:
    """Keeps one curl session per Profile, so connections are reused but never shared between Profiles.

    A session is created on first use, inside the running event loop, with curl's own
    retries off, proxies from the environment ignored, and HTTP/2 connections reused.
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
        self._entries: dict[str, _Entry] = {}  # least recently used first
        self._closing: set[asyncio.Task[None]] = set()

    @asynccontextmanager
    async def session(self, profile: str) -> AsyncGenerator[CurlSession]:
        """Use the session of a Profile, creating it if needed.

        Args:
            profile: The Profile, already checked.

        Yields:
            The session; it stays open while in use.
        """
        now = time.monotonic()
        entry = self._entries.pop(profile, None)
        if entry is not None and entry.users == 0 and now - entry.last_used > self._idle:
            self._retire(entry.session)
            entry = None
        if entry is None:
            entry = _Entry(self._new_session(profile))
        self._entries[profile] = entry
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
            curl_options={CurlOpt.PIPEWAIT: 1},
        )

    def _detach_evicted(self, now: float) -> list[CurlSession]:
        """Remove the sessions to close: the least recently used beyond `size`, and the idle ones."""
        unused = [profile for profile, entry in self._entries.items() if entry.users == 0]
        excess = max(0, len(self._entries) - self._size)
        evicted = unused[:excess] + [
            profile for profile in unused[excess:] if now - self._entries[profile].last_used > self._idle
        ]
        return [self._entries.pop(profile).session for profile in evicted]


async def _close(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:
        logger.exception("Closing a curl session failed")
