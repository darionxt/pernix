"""A Proxy Pool kept in this process, for one Run."""

from __future__ import annotations

import itertools
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.proxies.proxy_pool import ProxyCounts

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from pernix.proxies.proxy_pool import ProxyHealth
    from pernix.stats import Stats

__all__ = ["MemoryProxyPool"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    """A proxy as the pool holds it: whether it is listed, and its health."""

    url: str
    listed: bool = True
    failures: int = 0
    """Failures in a row."""
    failed_at: float = -math.inf
    """When the last failure counted, so the fetches of the same outage count once."""
    cool_until: float = 0.0
    retired: bool = False
    used: int = 0
    """When it was last taken or bound, as a count of those, so it goes to the back of the line."""

    def usable(self, now: float) -> bool:
        return self.listed and not self.retired and self.cool_until <= now


class MemoryProxyPool:
    """A Proxy Pool kept in this process, for one Run.

    Each fetch without a Session takes the available proxy used least recently, so
    the proxies take turns. A Session keeps the proxy it is bound to while that is
    available, and is otherwise bound to the one with the fewest Sessions, so
    Sessions share a proxy only when there are more of them than proxies. The pool
    holds the binding of each Session, so Leases of one Session that overlap, which
    loaded its state before either saved it, are given the same proxy. A failure
    cools its proxy down for `cooldown` seconds, doubled with each failure in a row,
    at most `cooldown_max`, and Retires it after `max_failures` in a row; a fetch
    that got through ends the row. A proxy that is no longer listed keeps its
    health, should it be listed again, and a Retired one stays Retired.
    """

    def __init__(
        self,
        *,
        cooldown: float = 60.0,
        cooldown_max: float = 600.0,
        max_failures: int = 5,
        stats: Stats | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the pool, with no proxy listed yet.

        Args:
            cooldown: Seconds a proxy cools down after its first failure in a row.
            cooldown_max: The longest a proxy cools down.
            max_failures: Failures in a row after which a proxy is Retired.
            stats: Where bindings, failures, cooldowns, and Retirements are
                counted, as `proxies.bindings`, `proxies.failures`,
                `proxies.cooldowns`, and `proxies.retirements`.
            clock: The current time, in seconds since the epoch.
        """
        self._cooldown = cooldown
        self._cooldown_max = cooldown_max
        self._max_failures = max_failures
        self._stats = stats
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._bound: dict[str, str] = {}
        """The proxy each Session is bound to, by its id."""
        self._uses = itertools.count(1)

    def health(self, proxy: str) -> ProxyHealth | None:
        """Return the health of a proxy.

        Args:
            proxy: The proxy.

        Returns:
            Its health, or `None` if the pool does not list it.
        """
        entry = self._entries.get(proxy)
        if entry is None or not entry.listed:
            return None
        if entry.retired:
            return "retired"
        return "cooldown" if entry.cool_until > self._clock() else "available"

    def counts(self) -> ProxyCounts:
        """Return how many of the proxies listed are in each state.

        Returns:
            The counts.
        """
        now = self._clock()
        listed = [entry for entry in self._entries.values() if entry.listed]
        retired = sum(entry.retired for entry in listed)
        available = sum(entry.usable(now) for entry in listed)
        return ProxyCounts(available=available, cooldown=len(listed) - retired - available, retired=retired)

    def __len__(self) -> int:
        return sum(entry.listed for entry in self._entries.values())

    async def replace(self, proxies: Collection[str]) -> None:
        """List the proxies the sources gave, in place of those listed before.

        Args:
            proxies: The proxies.
        """
        listed = frozenset(proxies)
        for url, entry in self._entries.items():
            entry.listed = url in listed
        for url in proxies:
            self._entries.setdefault(url, _Entry(url))

    async def take(self) -> str | None:
        """Take a proxy for one fetch: the available one used least recently.

        Returns:
            The proxy, or `None` if none is available now.
        """
        now = self._clock()
        usable = [entry for entry in self._entries.values() if entry.usable(now)]
        if not usable:
            return None
        entry = min(usable, key=lambda entry: entry.used)
        entry.used = next(self._uses)
        return entry.url

    async def bind(self, session: str, current: str | None) -> str | None:
        """Return the proxy a Session goes through: its own while available, else the one with the fewest Sessions.

        Args:
            session: The Session's id.
            current: The proxy the Session's state names, for a Session the pool
                bound to none yet; the pool's binding goes first.

        Returns:
            The proxy, or `None` if none is available now.
        """
        now = self._clock()
        url = self._bound.get(session, current)
        held = None if url is None else self._entries.get(url)
        if held is not None and held.usable(now):
            self._bound[session] = held.url
            return held.url
        self._bound.pop(session, None)
        usable = [entry for entry in self._entries.values() if entry.usable(now)]
        if not usable:
            return None
        sessions = Counter(self._bound.values())
        entry = min(usable, key=lambda entry: (sessions[entry.url], entry.used))
        entry.used = next(self._uses)
        self._bound[session] = entry.url
        self._count("proxies.bindings")
        return entry.url

    async def report(self, proxy: str, *, failed: bool, since: float) -> None:
        """Report how a fetch through a proxy went, cooling it down or Retiring it after a failure.

        Args:
            proxy: The proxy; one the pool does not hold is ignored.
            failed: Whether the proxy failed the fetch.
            since: When the fetch took the proxy, in seconds since the epoch.
        """
        entry = self._entries.get(proxy)
        if entry is None:
            return
        if failed:
            self._count("proxies.failures")
        if since < entry.failed_at or entry.retired:
            return  # a fetch of the outage already counted
        if not failed:
            entry.failures = 0
            return
        now = self._clock()
        entry.failures += 1
        entry.failed_at = now
        if entry.failures >= self._max_failures:
            entry.retired = True
            self._count("proxies.retirements")
            logger.warning("Retired a proxy after %d failures in a row", entry.failures)
            return
        entry.cool_until = now + min(self._cooldown * 2 ** (entry.failures - 1), self._cooldown_max)
        self._count("proxies.cooldowns")

    async def ready_in(self) -> float:
        """Return how long the proxies listed are all cooling down for.

        Returns:
            Seconds until one is available; 0 if one is, and `math.inf` if none ever
            will be, as every one listed is Retired, or none is.
        """
        waiting = [entry.cool_until for entry in self._entries.values() if entry.listed and not entry.retired]
        if not waiting:
            return math.inf
        return max(0.0, min(waiting) - self._clock())

    def _count(self, key: str) -> None:
        if self._stats is not None:
            self._stats.inc(key)
