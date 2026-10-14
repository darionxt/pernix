from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from curl_cffi import Curl, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession

from pernix.errors import ResponseTooLarge

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

__all__ = ["CurlSession", "CurlSessionPool", "Transfer"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Transfer:
    """The curl handle of one request, and how much body it delivered.

    Attributes:
        curl: The handle.
        limit: Bytes of body it may deliver; `None` for no limit.
        received: Bytes of body delivered so far, decoded.
        exceeded: Whether the limit stopped it.
        returned: Whether the handle went back to the session's pool.
    """

    curl: Curl
    limit: int | None
    received: int = 0
    exceeded: bool = False
    returned: bool = False


@dataclass(slots=True)
class _Start:
    limit: int | None
    transfer: Transfer | None = None


_starting: ContextVar[_Start | None] = ContextVar("pernix_curl_start", default=None)
"""The request the current task is starting with `CurlSession.start()`, if any."""


class CurlSession(AsyncSession):
    """A curl-cffi session that limits each streamed body as curl receives it, and stops abandoned transfers.

    curl-cffi queues a streamed body without bound, and runs the transfer in a task of
    its own, which goes on when the request waiting for the headers is cancelled.
    `start()` therefore notes the handle a request takes, through the `pop_curl()` and
    `push_curl()` hooks of curl-cffi's handle pool: it counts the body in the handle's
    write callback, failing the transfer past the limit, and removes the handle from
    curl when the request is cancelled.
    """

    def __init__(self, **options: Any) -> None:
        """Create the session.

        Args:
            **options: The options of `curl_cffi.requests.AsyncSession`.
        """
        super().__init__(**options)
        self._transfers: dict[Curl, Transfer] = {}

    async def start(
        self, method: HttpMethod, url: str, *, body_limit: int | None, **options: Any
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Start a streamed transfer and wait for its headers.

        Args:
            method: The HTTP method.
            url: The URL.
            body_limit: Bytes of body the transfer may deliver; `None` for no limit.
            **options: The options of `AsyncSession.request()`, but `stream`.

        Returns:
            The response, its body still to read, and the transfer.

        Raises:
            curl_cffi.CurlError: The transfer failed before the headers arrived.
            pernix.errors.ResponseTooLarge: The limit stopped the transfer before the
                headers came back.
        """
        start = _Start(body_limit)
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
            if start.limit is not None:
                setattr(curl, "setopt", _counting(curl.setopt, transfer))  # noqa: B010  # before curl-cffi sets it up
        return curl

    def push_curl(self, curl: Curl | None) -> None:
        """Return a handle at the end of its transfer."""
        if curl is not None and (transfer := self._transfers.pop(curl, None)) is not None:
            transfer.returned = True
            vars(curl).pop("setopt", None)
        super().push_curl(curl)


def _counting(setopt: Callable[[CurlOpt, Any], int], transfer: Transfer) -> Callable[[CurlOpt, Any], int]:
    """Wrap a handle's `setopt()` so that the write callback it installs counts the body."""

    def counting_setopt(option: CurlOpt, value: Any) -> int:
        if option == CurlOpt.WRITEFUNCTION and callable(value):
            write: Callable[[bytes], Any] = value
            limit = transfer.limit or 0

            def counted(chunk: bytes) -> Any:
                transfer.received += len(chunk)
                if transfer.received > limit:
                    transfer.exceeded = True
                    return CURL_WRITEFUNC_ERROR  # curl stops receiving at once
                return write(chunk)

            value = counted
        return setopt(option, value)

    return counting_setopt


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
