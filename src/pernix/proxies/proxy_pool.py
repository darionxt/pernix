"""The Proxy Pool protocol: the proxies Requests are sent through, their health, and the Sessions bound to them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pernix.proxies.proxy_url import proxy_address

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from pernix.proxies.proxy import Proxy

__all__ = ["ProxyCandidate", "ProxyCounts", "ProxyHealth", "ProxyPool"]

type ProxyHealth = Literal["available", "cooldown", "retired"]
"""Whether a proxy can be used: available, cooling down until a time, or retired for good."""


@dataclass(frozen=True, slots=True)
class ProxyCounts:
    """How many of the proxies listed are in each state.

    Attributes:
        available: Proxies that can be used now.
        cooldown: Proxies cooling down after a failure.
        retired: Proxies retired after too many failures in a row.
    """

    available: int
    cooldown: int
    retired: int


@dataclass(frozen=True, slots=True)
class ProxyCandidate:
    """A proxy the pool lists, as a binding policy sees it.

    Its `repr` shows it without its credentials.

    Attributes:
        url: The proxy URL, which a binding policy returns to bind it.
        tags: Its tags, as its source gave them.
        health: Whether it can be used now.
        sessions: The ids of the Sessions bound to it.
        failures: Its failures in a row.
        expires: When it stops working, as its source tells, in seconds since the
            epoch; `None` if it does not tell.
    """

    url: str
    tags: frozenset[str] = frozenset()
    health: ProxyHealth = "available"
    sessions: frozenset[str] = frozenset()
    failures: int = 0
    expires: float | None = None

    def __repr__(self) -> str:
        return f"ProxyCandidate({proxy_address(self.url)!r}, health={self.health!r}, sessions={len(self.sessions)})"


class ProxyPool(Protocol):
    """The proxies Requests are sent through, with their health, and the Sessions bound to them.

    The proxies are those last listed by the sources; a proxy no longer listed is
    not used, but its health is kept, should it be listed again. A proxy that fails
    cools down, longer with each failure in a row, and is Retired after too many.
    Sessions are bound to proxies as a binding policy picks among the
    `candidates()`, and the pool holds each binding, binding no more than
    `max_sessions` Sessions to a proxy. Proxies are URLs as
    `pernix.proxies.proxy_url()` gives them. The in-process implementation is
    `pernix.proxies.MemoryProxyPool`; one shared by several processes keeps the
    same protocol.

    Attributes:
        max_sessions: Sessions a proxy is bound to at most; `None` for no limit.
    """

    max_sessions: int | None

    async def replace(self, proxies: Collection[Proxy]) -> None:
        """List the proxies the sources gave, in place of those listed before.

        A proxy listed again keeps its health, and takes the tags and the expiry
        given now.

        Args:
            proxies: The proxies, with URLs as `proxy_url()` gives them.
        """
        ...

    async def take(self) -> str | None:
        """Take a proxy for one fetch: the available one used least recently.

        Returns:
            The proxy, or `None` if none is available now.
        """
        ...

    async def candidates(self) -> Sequence[ProxyCandidate]:
        """Return the proxies listed, as a binding policy sees them, least recently used first.

        A proxy is used when a fetch takes it, and when a Session is bound to it in
        place of another. Reading them changes nothing.

        Returns:
            Every proxy listed, whatever its health.
        """
        ...

    async def bind(self, session: str, proxy: str) -> bool:
        """Bind a Session to a proxy, in place of the one it was bound to, if the proxy is available and has room for it.

        A proxy has room for a Session bound to it already, and for another while
        fewer than `max_sessions` are. The pool holds the binding of each Session,
        so that Leases of one Session that overlap go through the same proxy,
        though each loaded its state before the other saved it.

        Args:
            session: The Session's id.
            proxy: The proxy, one of the `candidates()`.

        Returns:
            Whether the Session is bound to the proxy now; `False`, changing nothing,
            if the proxy is not listed, not available, or has no room, as when
            another Session took the last since the candidates were read.
        """
        ...

    async def unbind(self, session: str) -> None:
        """Release the binding of a Session, such as one Retired, so it no longer counts toward its proxy's Sessions.

        Args:
            session: The Session's id; one bound to no proxy is ignored.
        """
        ...

    async def report(self, proxy: str, *, failed: bool, since: float) -> None:
        """Report how a fetch through a proxy went.

        A failure cools the proxy down, or Retires it after too many in a row; a
        fetch that got through ends the row. A fetch that started before the last
        failure counted tells of the same outage, and changes nothing.

        Args:
            proxy: The proxy; one the pool does not hold is ignored.
            failed: Whether the proxy failed the fetch.
            since: When the fetch took the proxy, in seconds since the epoch.
        """
        ...

    async def ready_in(self) -> float:
        """Return how long the proxies listed are all cooling down for.

        Returns:
            Seconds until one is available; 0 if one is, and `math.inf` if none ever
            will be, as every one listed is Retired, or none is.
        """
        ...
