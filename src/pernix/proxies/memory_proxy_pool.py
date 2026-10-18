"""A Proxy Pool kept in this process, for one Run."""

from __future__ import annotations

import itertools
import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.proxies.proxy import Proxy
from pernix.proxies.proxy_pool import ProxyCandidate, ProxyCounts

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from pernix.proxies.proxy_pool import ProxyHealth
    from pernix.stats import Stats

__all__ = ["MemoryProxyPool"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    """A proxy as the pool holds it: whether it is listed or a Session's own, and its health."""

    url: str
    tags: frozenset[str] = frozenset()
    expires: float | None = None
    listed: bool = True
    owner: str | None = None
    """The Session last given it as its own, which holds it while it is bound to it, or its Leases go through it."""
    failures: int = 0
    """Failures in a row."""
    failed_at: float = -math.inf
    """When the last failure counted, so the fetches of the same outage count once."""
    cool_until: float = 0.0
    retired: bool = False
    used: int = 0
    """When it was last taken or bound, as a count of those, so it goes to the back of the line."""
    expiry_counted: bool = False
    """Whether its expiry was counted, so it counts once until a source gives it a later one."""

    def expired(self, now: float) -> bool:
        return self.expires is not None and self.expires <= now

    def available(self, now: float) -> bool:
        return not self.retired and not self.expired(now) and self.cool_until <= now

    def health(self, now: float) -> ProxyHealth:
        if self.retired:
            return "retired"
        if self.expired(now):
            return "expired"
        return "cooldown" if self.cool_until > now else "available"

    def renew(self, proxy: Proxy, now: float) -> None:
        """Take the tags and the expiry a source, or a binding policy, gives the proxy now."""
        self.tags, self.expires = proxy.tags, proxy.expires
        if not self.expired(now):
            self.expiry_counted = False


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
    proxy. A Session given a proxy of its own holds it while it is bound to it, or
    its Leases go through it: no fetch takes it, no other Session sees it among
    the candidates or is bound to it, even should a source list it, and
    `max_sessions` does not apply. A failure cools its proxy down for `cooldown`
    seconds, doubled with each failure in a row, at most `cooldown_max`, and
    Retires it after `max_failures` in a row; a fetch that got through ends the
    row. A proxy that is no longer listed, or held, keeps its health, should it be
    listed or held again, and a Retired one stays Retired. A proxy whose `expires`
    passed is expired: no fetch takes it and no Session is bound to it, and it is
    among the candidates only while Sessions occupy it; then the pool forgets it,
    but for a Retired one, which stays Retired should a source give it again.

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
            stats: Where bindings, failures, cooldowns, Retirements, and
                expirations are counted, as `proxies.bindings`, `proxies.failures`,
                `proxies.cooldowns`, `proxies.retirements`, and
                `proxies.expirations`.
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
            Its health, or `None` if the pool neither lists it nor holds it as a
            Session's own, or forgot it once it expired.
        """
        now = self._clock()
        self._forget_expired(now)
        entry = self._entries.get(proxy)
        if entry is None or not (entry.listed or self._holder(entry) is not None):
            return None
        return entry.health(now)

    def counts(self) -> ProxyCounts:
        """Return how many of the proxies listed, and of the Sessions' own in use, are in each state, but expired.

        Returns:
            The counts.
        """
        now = self._clock()
        used = [entry for entry in self._entries.values() if self._in_use(entry, now)]
        retired = sum(entry.retired for entry in used)
        available = sum(entry.available(now) for entry in used)
        return ProxyCounts(available=available, cooldown=len(used) - retired - available, retired=retired)

    def __len__(self) -> int:
        now = self._clock()
        return sum(self._in_use(entry, now) for entry in self._entries.values())

    async def replace(self, proxies: Collection[Proxy]) -> None:
        """List the proxies the sources gave, in place of those listed before.

        A proxy listed again keeps its health, and takes the tags and the expiry
        given now; a proxy given twice keeps the first.

        Args:
            proxies: The proxies, with URLs as `proxy_url()` gives them.
        """
        now = self._clock()
        self._forget_expired(now)
        listed = _first_of_each(proxies)
        for url, entry in self._entries.items():
            entry.listed = url in listed
        for url, proxy in listed.items():
            self._entries.setdefault(url, _Entry(url)).renew(proxy, now)

    async def add(self, proxies: Collection[Proxy]) -> None:
        """List more proxies, keeping those listed until they expire.

        A proxy listed again, or added again, keeps its health, and takes the tags
        and the expiry given now; a proxy given twice keeps the first.

        Args:
            proxies: The proxies, with URLs as `proxy_url()` gives them.
        """
        now = self._clock()
        self._forget_expired(now)
        for url, proxy in _first_of_each(proxies).items():
            entry = self._entries.setdefault(url, _Entry(url))
            entry.listed = True
            entry.renew(proxy, now)

    async def take(self) -> str | None:
        """Take a proxy for one fetch: the available one used least recently, of those listed and no Session's own.

        Returns:
            The proxy, or `None` if none is available now.
        """
        now = self._clock()
        self._forget_expired(now)
        usable = [entry for entry in self._entries.values() if self._shared(entry) and entry.available(now)]
        if not usable:
            return None
        entry = min(usable, key=lambda entry: entry.used)
        entry.used = next(self._uses)
        return entry.url

    async def candidates(self, session: str | None = None) -> tuple[ProxyCandidate, ...]:
        """Return the proxies a Session may be bound to, as a binding policy sees them, least recently used first.

        Args:
            session: The id of the Session, whose own proxy, if it holds one, is
                among them; `None` for the proxies listed only.

        Returns:
            Every proxy listed, whatever its health, but the other Sessions' own, and
            the Session's own; an expired one only while Sessions occupy it.
        """
        now = self._clock()
        self._forget_expired(now)
        bound: defaultdict[str, set[str]] = defaultdict(set)
        for each, url in self._bound.items():
            bound[url].add(each)
        leaving: defaultdict[str, set[str]] = defaultdict(set)
        for each, url in self._leases:
            if self._bound.get(each) != url:
                leaving[url].add(each)
        candidates: list[ProxyCandidate] = []
        for entry in sorted(self._entries.values(), key=lambda entry: entry.used):
            holder = self._holder(entry)
            if not (entry.listed if holder is None else holder == session):
                continue  # not listed, or another Session's own
            if entry.expired(now) and not (bound[entry.url] or leaving[entry.url]):
                continue  # it left the pool, though a Retired one is remembered
            health = entry.health(now)
            candidates.append(
                ProxyCandidate(
                    entry.url,
                    tags=entry.tags,
                    health=health,
                    sessions=frozenset(bound[entry.url]),
                    leaving=frozenset(leaving[entry.url]),
                    failures=entry.failures,
                    expires=entry.expires,
                    until=entry.cool_until if health == "cooldown" else None,
                    own=holder is not None,
                )
            )
        return tuple(candidates)

    async def bind(self, session: str, proxy: str | Proxy) -> bool:
        """Bind a Session to a proxy for one of its Leases, in place of the one it was bound to, if the proxy has room.

        Args:
            session: The Session's id.
            proxy: The URL of one of its candidates, or a `Proxy` record of its own.

        Returns:
            Whether the Session is bound to the proxy now; `False`, changing nothing,
            if the proxy is not one of its candidates, not available, or has no
            room, or for a record, if the pool lists the proxy, holds it for
            another Session, or other Sessions still occupy it.
        """
        own = isinstance(proxy, Proxy)
        url = proxy.url if isinstance(proxy, Proxy) else proxy
        entry = self._entries.get(url)
        holder = None if entry is None else self._holder(entry)
        if holder not in (None, session):
            return False
        if entry is None:
            if not own:
                return False
            entry = _Entry(url, listed=False)
        elif holder is None and entry.listed == own:
            return False  # a record of a proxy the pool lists, or a URL it neither lists nor holds for the Session
        elif own and holder is None and self._occupants(url) - {session}:
            return False  # Sessions bound to it while it was listed still go through it
        now = self._clock()
        expires = proxy.expires if isinstance(proxy, Proxy) else entry.expires
        if entry.retired or entry.cool_until > now or (expires is not None and expires <= now):
            return False
        shared = holder is None and not own
        if shared and self.max_sessions is not None and len(self._occupants(url) - {session}) >= self.max_sessions:
            return False
        if isinstance(proxy, Proxy):
            entry.owner = session
            entry.renew(proxy, now)
            self._entries.setdefault(url, entry)
        elif shared:
            entry.owner = None  # a Session that held it as its own before shares it now, as the others do
        if self._bound.get(session) != url:
            entry.used = next(self._uses)
            self._bound[session] = url
            self._count("proxies.bindings")
        self._leases[session, url] += 1
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
        """Return how long the proxies listed, which no Session holds as its own, are all cooling down for.

        Returns:
            Seconds until one is available; 0 if one is, and `math.inf` if none ever
            will be, as every one listed is Retired or expired, or none is.
        """
        now = self._clock()
        waiting = [
            entry.cool_until
            for entry in self._entries.values()
            if self._shared(entry) and not entry.retired and not entry.expired(now)
        ]
        if not waiting:
            return math.inf
        return max(0.0, min(waiting) - now)

    def _occupants(self, proxy: str) -> set[str]:
        """The Sessions that count toward a proxy's `max_sessions`: bound to it, or with Leases through it."""
        occupants = {session for session, url in self._bound.items() if url == proxy}
        occupants.update(session for session, url in self._leases if url == proxy)
        return occupants

    def _holder(self, entry: _Entry) -> str | None:
        """The Session holding a proxy as its own: the one last given it, while it is bound to it or goes through it."""
        owner = entry.owner
        if owner is not None and (self._bound.get(owner) == entry.url or (owner, entry.url) in self._leases):
            return owner
        return None

    def _shared(self, entry: _Entry) -> bool:
        """Whether a proxy is the pool's to give: listed, and held by no Session as its own."""
        return entry.listed and self._holder(entry) is None

    def _in_use(self, entry: _Entry, now: float) -> bool:
        """Whether a proxy is listed, or held by a Session as its own, and not expired."""
        return (entry.listed or self._holder(entry) is not None) and not entry.expired(now)

    def _forget_expired(self, now: float) -> None:
        """Count the proxies in use that expired, and forget those no Session occupies, but for the Retired."""
        occupied = set(self._bound.values()) | {url for _, url in self._leases}
        for url, entry in list(self._entries.items()):
            if not entry.expired(now):
                continue
            if not entry.expiry_counted:
                entry.expiry_counted = True
                if entry.listed or entry.owner is not None:
                    self._count("proxies.expirations")
            if url not in occupied and not entry.retired:
                del self._entries[url]

    def _count(self, key: str) -> None:
        if self._stats is not None:
            self._stats.inc(key)


def _first_of_each(proxies: Collection[Proxy]) -> dict[str, Proxy]:
    """The proxies by URL, the first given of each kept."""
    first: dict[str, Proxy] = {}
    for proxy in proxies:
        first.setdefault(proxy.url, proxy)
    return first
