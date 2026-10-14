from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from curl_cffi import Curl, CurlOpt
from curl_cffi.requests import AsyncSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["CurlSession", "CurlSessionPool"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Checkout:
    """A curl handle taken from a session's pool for one transfer."""

    curl: Curl
    returned: bool = False


_checkout: ContextVar[_Checkout | None] = ContextVar("pernix_curl_checkout", default=None)
"""The handle the current task last took from a `CurlSession`."""


class CurlSession(AsyncSession):
    """A curl-cffi session that can stop a streamed transfer cancelled before its headers arrived.

    curl-cffi runs a streamed transfer in a task of its own, which goes on when the
    request waiting for the headers is cancelled. This session notes the handle each
    request takes, through the `pop_curl()` and `push_curl()` hooks of curl-cffi's
    handle pool, so that `abort_pending()` can remove it from curl.
    """

    def __init__(self, **options: Any) -> None:
        """Create the session.

        Args:
            **options: The options of `curl_cffi.requests.AsyncSession`.
        """
        super().__init__(**options)
        self._checkouts: dict[Curl, _Checkout] = {}

    async def pop_curl(self) -> Curl:
        """Take a handle for a transfer, noting it as the current task's."""
        curl = await super().pop_curl()
        checkout = _Checkout(curl)
        self._checkouts[curl] = checkout
        _checkout.set(checkout)
        return curl

    def push_curl(self, curl: Curl | None) -> None:
        """Return a handle at the end of its transfer."""
        if curl is not None and (checkout := self._checkouts.pop(curl, None)) is not None:
            checkout.returned = True
        super().push_curl(curl)

    def abort_pending(self) -> None:
        """Stop the transfer of the current task's last request, unless its handle was returned already.

        Call it where that request was cancelled; curl-cffi then ends the transfer and
        returns the handle.
        """
        checkout = _checkout.get()
        if checkout is not None and not checkout.returned and self._checkouts.get(checkout.curl) is checkout:
            self.acurl.remove_handle(checkout.curl)


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
    then replaced; both are checked whenever a session is taken.
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

    @asynccontextmanager
    async def session(self, profile: str) -> AsyncGenerator[CurlSession]:
        """Use the session of a Profile, creating it if needed.

        Args:
            profile: The Profile, already checked.

        Yields:
            The session; it stays open while in use.
        """
        now = time.monotonic()
        closing: list[_Entry] = []
        entry = self._entries.pop(profile, None)
        if entry is not None and entry.users == 0 and now - entry.last_used > self._idle:
            closing.append(entry)
            entry = None
        if entry is None:
            entry = _Entry(self._new_session(profile))
        self._entries[profile] = entry
        entry.users += 1
        try:
            closing += self._detach_evicted(now)  # detached first, so no other task can take them meanwhile
            for evicted in closing:
                await _close(evicted.session)
            yield entry.session
        finally:
            entry.users -= 1
            entry.last_used = time.monotonic()

    async def close(self) -> None:
        """Close every session."""
        entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            await _close(entry.session)

    def _new_session(self, profile: str) -> CurlSession:
        return CurlSession(
            max_clients=self._max_clients,
            impersonate=profile,
            trust_env=False,
            retry=0,
            curl_options={CurlOpt.PIPEWAIT: 1},
        )

    def _detach_evicted(self, now: float) -> list[_Entry]:
        """Remove the sessions to close: the least recently used beyond `size`, and the idle ones."""
        unused = [profile for profile, entry in self._entries.items() if entry.users == 0]
        excess = max(0, len(self._entries) - self._size)
        evicted = unused[:excess] + [
            profile for profile in unused[excess:] if now - self._entries[profile].last_used > self._idle
        ]
        return [self._entries.pop(profile) for profile in evicted]


async def _close(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:
        logger.exception("Closing a curl session failed")
