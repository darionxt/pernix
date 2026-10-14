from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from curl_cffi import CurlOpt
from curl_cffi.requests import AsyncSession

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["CurlSessionPool"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    session: AsyncSession
    users: int = 0
    last_used: float = field(default_factory=time.monotonic)


class CurlSessionPool:
    """Keeps one curl-cffi session per Profile, so connections are reused but never shared between Profiles.

    A session is created on first use, inside the running event loop, with curl's own
    retries off, proxies from the environment ignored, and HTTP/2 connections reused.
    Beyond `size` sessions, the least recently used one not in use is closed, and so
    is one unused for `idle` seconds; both are checked whenever a session is taken.
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
    async def session(self, profile: str) -> AsyncGenerator[AsyncSession]:
        """Use the session of a Profile, creating it if needed.

        Args:
            profile: The Profile, already checked.

        Yields:
            The session; it stays open while in use.
        """
        entry = self._entries.pop(profile, None)
        if entry is None:
            entry = _Entry(self._new_session(profile))
        self._entries[profile] = entry
        entry.users += 1
        try:
            await self._evict()
            yield entry.session
        finally:
            entry.users -= 1
            entry.last_used = time.monotonic()

    async def close(self) -> None:
        """Close every session."""
        entries, self._entries = list(self._entries.values()), {}
        for entry in entries:
            await _close(entry.session)

    def _new_session(self, profile: str) -> AsyncSession:
        return AsyncSession(
            max_clients=self._max_clients,
            impersonate=profile,
            trust_env=False,
            retry=0,
            curl_options={CurlOpt.PIPEWAIT: 1},
        )

    async def _evict(self) -> None:
        now = time.monotonic()
        unused = [profile for profile, entry in self._entries.items() if entry.users == 0]
        excess = max(0, len(self._entries) - self._size)
        evicted = unused[:excess] + [
            profile for profile in unused[excess:] if now - self._entries[profile].last_used > self._idle
        ]
        for profile in evicted:
            await _close(self._entries.pop(profile).session)


async def _close(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:
        logger.exception("Closing a curl session failed")
