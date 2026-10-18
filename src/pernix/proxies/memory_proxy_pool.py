"""A Proxy Pool kept in this process, for one Run."""

from __future__ import annotations

import itertools
import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.proxies.proxy_pool import ProxyCandidate, ProxyCounts

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from pernix.proxies.proxy import Proxy
    from pernix.proxies.proxy_pool import ProxyHealth
    from pernix.stats import Stats

__all__ = ["MemoryProxyPool"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    """A proxy as the pool holds it: whether it is listed, and its health."""

    url: str
    tags: frozenset[str] = frozenset()
    expires: float | None = None
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

    def health(self, now: float) -> ProxyHealth:
        if self.retired:
            return "retired"
        return "cooldown" if self.cool_until > now else "available"


class MemoryProxyPool:
    """A Proxy Pool kept in this process, for one Run.

    Each fetch without a Session takes the available proxy used least recently, so
    the proxies take turns. Sessions are bound as a binding policy picks among the
    `candidates()`, listed least recently used first; a Session is bound only to a
    proxy that is available and has room for it, with fewer than `max_sessions`
    other Sessions, so bindings made at once cannot exceed it. A Session bound to
    another proxy still counts toward the one its Leases went through until they
    end, so that two Sessions never go through one proxy at once beyond the limit.
    The pool holds the binding of each Session, so Leases of one Session that
    overlap, which loaded its state before either saved it, are given the same
    proxy. A failure cools its
    proxy down for `cooldown` seconds, doubled with each failure in a row, at most
    `cooldown_max`, and Retires it after `max_failures` in a row; a fetch that got
    through ends the row. A proxy that is no longer listed keeps its health, should
    it be listed again, and a Retired one stays Retired.

    Attributes:
        max_sessions: Sessions a proxy is bound to at most; `None` for no limit.
    """

    def __init__(
        self,
        *,
        max_sessions: int | None = None,
        cooldown: float = 60.0,
        cooldown_max: float = 600.0,
        max_failures: int = 5,
        stats: Stats | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the pool, with no proxy listed yet.

        Args:
            max_sessions: Sessions a proxy is bound to at most; `None` for no limit.
            cooldown: Seconds a proxy cools down after its first failure in a row.
            cooldown_max: The longest a proxy cools down.
            max_failures: Failures in a row after which a proxy is Retired.
            stats: Where bindings, failures, cooldowns, and Retirements are
                counted, as `proxies.bindings`, `proxies.failures`,
                `proxies.cooldowns`, and `proxies.retirements`.
            clock: The current time, in seconds since the epoch.
        """
        self.max_sessions = max_sessions
        self._cooldown = cooldown
        self._cooldown_max = cooldown_max
        self._max_failures = max_failures
        self._stats = stats
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._bound: dict[str, str] = {}
        """The proxy each Session is bound to, by its id."""
        self._leases: Counter[tuple[str, str]] = Counter()
        """The Leases going through each proxy, by the id of their Session and the proxy."""
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
        return entry.health(self._clock())

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

    async def replace(self, proxies: Collection[Proxy]) -> None:
        """List the proxies the sources gave, in place of those listed before.

        A proxy listed again keeps its health, and takes the tags and the expiry
        given now; a proxy given twice keeps the first.

        Args:
            proxies: The proxies, with URLs as `proxy_url()` gives them.
        """
        listed: dict[str, Proxy] = {}
        for proxy in proxies:
            listed.setdefault(proxy.url, proxy)
        for url, entry in self._entries.items():
            entry.listed = url in listed
        for url, proxy in listed.items():
            entry = self._entries.setdefault(url, _Entry(url))
            entry.tags, entry.expires = proxy.tags, proxy.expires

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

    async def candidates(self) -> tuple[ProxyCandidate, ...]:
        """Return the proxies listed, as a binding policy sees them, least recently used first.

        Returns:
            Every proxy listed, whatever its health.
        """
        now = self._clock()
        bound: defaultdict[str, set[str]] = defaultdict(set)
        for session, url in self._bound.items():
            bound[url].add(session)
        leaving: defaultdict[str, set[str]] = defaultdict(set)
        for session, url in self._leases:
            if self._bound.get(session) != url:
                leaving[url].add(session)
        listed = sorted((entry for entry in self._entries.values() if entry.listed), key=lambda entry: entry.used)
        return tuple(
            ProxyCandidate(
                entry.url,
                tags=entry.tags,
                health=entry.health(now),
                sessions=frozenset(bound[entry.url]),
                leaving=frozenset(leaving[entry.url]),
                failures=entry.failures,
                expires=entry.expires,
            )
            for entry in listed
        )

    async def bind(self, session: str, proxy: str) -> bool:
        """Bind a Session to a proxy for one of its Leases, in place of the one it was bound to, if the proxy has room.

        Args:
            session: The Session's id.
            proxy: The proxy.

        Returns:
            Whether the Session is bound to the proxy now; `False`, changing nothing,
            if the proxy is not listed, not available, or has no room.
        """
        entry = self._entries.get(proxy)
        if entry is None or not entry.usable(self._clock()):
            return False
        if self.max_sessions is not None and len(self._occupants(proxy) - {session}) >= self.max_sessions:
            return False
        if self._bound.get(session) != proxy:
            entry.used = next(self._uses)
            self._bound[session] = proxy
            self._count("proxies.bindings")
        self._leases[session, proxy] += 1
        return True

    async def release(self, session: str, proxy: str) -> None:
        """End the hold on a proxy of a Lease that `bind()` bound a Session to it for.

        Args:
            session: The Session's id.
            proxy: The proxy the Lease was bound to.
        """
        self._leases[session, proxy] -= 1
        if self._leases[session, proxy] <= 0:
            del self._leases[session, proxy]

    async def unbind(self, session: str) -> None:
        """Release the binding of a Session, so it no longer counts toward its proxy's Sessions once its Leases end.

        Args:
            session: The Session's id; one bound to no proxy is ignored.
        """
        self._bound.pop(session, None)

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

    def _occupants(self, proxy: str) -> set[str]:
        """The Sessions that count toward a proxy's `max_sessions`: bound to it, or with Leases through it."""
        occupants = {session for session, url in self._bound.items() if url == proxy}
        occupants.update(session for session, url in self._leases if url == proxy)
        return occupants

    def _count(self, key: str) -> None:
        if self._stats is not None:
            self._stats.inc(key)
