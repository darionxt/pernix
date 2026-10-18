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
    """How many of the proxies listed, and of the Sessions' own in use, are in each state.

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
    """A proxy the pool lists, or a Session's own, as a binding policy sees it.

    Its `repr` shows it without its credentials.

    Attributes:
        url: The proxy URL, which a binding policy returns to bind it.
        tags: Its tags, as its source, or the binding policy for a Session's own,
            gave them.
        health: Whether it can be used now.
        sessions: The ids of the Sessions bound to it.
        leaving: The ids of the Sessions bound to another proxy since, whose
            Leases still go through this one.
        failures: Its failures in a row.
        expires: When it stops working, as its source tells, in seconds since the
            epoch; `None` if it does not tell.
        until: When it stops cooling down, in seconds since the epoch; `None` unless
            it cools down.
        own: Whether it is the Session's own, which the binding policy gave it and
            no other Session is bound to.
    """

    url: str
    tags: frozenset[str] = frozenset()
    health: ProxyHealth = "available"
    sessions: frozenset[str] = frozenset()
    leaving: frozenset[str] = frozenset()
    failures: int = 0
    expires: float | None = None
    until: float | None = None
    own: bool = False

    @property
    def occupants(self) -> frozenset[str]:
        """The Sessions that count toward its `max_sessions`: those bound to it, and those leaving it."""
        return self.sessions | self.leaving

    def __repr__(self) -> str:
        own = ", own=True" if self.own else ""
        return (
            f"ProxyCandidate({proxy_address(self.url)!r}, health={self.health!r}, sessions={len(self.sessions)}{own})"
        )


class ProxyPool(Protocol):
    """The proxies Requests are sent through, with their health, and the Sessions bound to them.

    The proxies are those last listed by the sources; a proxy no longer listed is
    not used, but its health is kept, should it be listed again. A proxy that fails
    cools down, longer with each failure in a row, and is Retired after too many.
    Sessions are bound to proxies as a binding policy picks among the
    `candidates()`, and the pool holds each binding, binding no more than
    `max_sessions` Sessions to a proxy; a Session bound to another counts toward a
    proxy until the Leases that went through it end. A binding policy may also
    give a Session a proxy of its own, which the pool holds for that Session
    alone while it is bound to it, or its Leases go through it: it takes no
    turns, no other Session is bound to it, and refreshes keep it; its health is
    kept as any other's. Proxies are URLs as `pernix.proxies.proxy_url()` gives
    them. The in-process implementation is `pernix.proxies.MemoryProxyPool`; one
    shared by several processes keeps the same protocol.

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
        """Take a proxy for one fetch: the available one used least recently, of those listed and no Session's own.

        Returns:
            The proxy, or `None` if none is available now.
        """
        ...

    async def candidates(self, session: str | None = None) -> Sequence[ProxyCandidate]:
        """Return the proxies a Session may be bound to, as a binding policy sees them, least recently used first.

        A proxy is used when a fetch takes it, and when a Session is bound to it in
        place of another. Reading them changes nothing.

        Args:
            session: The id of the Session, whose own proxy, if it holds one, is
                among them; `None` for the proxies listed only.

        Returns:
            Every proxy listed, whatever its health, but the other Sessions' own, and
            the Session's own.
        """
        ...

    async def bind(self, session: str, proxy: str | Proxy) -> bool:
        """Bind a Session to a proxy for one of its Leases, in place of the one it was bound to, if the proxy has room.

        A proxy has room for a Session while it is available, and while fewer than
        `max_sessions` other Sessions occupy it: those bound to it, and those bound
        to another since whose Leases still go through it. A Session's own has room
        for it alone. The Lease holds the proxy until `release()`. The pool holds
        the binding of each Session, so that Leases of one Session that overlap go
        through the same proxy, though each loaded its state before the other saved
        it.

        Args:
            session: The Session's id.
            proxy: The URL of one of its `candidates()`; or a `Proxy` record of its
                own, which the pool holds for it from now on, keeping the health of
                one it held before, and taking the tags and the expiry given.

        Returns:
            Whether the Session is bound to the proxy now; `False`, changing nothing,
            if the proxy is not one of its candidates, not available, or has no
            room, as when another Session took the last since the candidates were
            read, or for a record, if the pool lists the proxy, or holds it for
            another Session.
        """
        ...

    async def release(self, session: str, proxy: str) -> None:
        """End the hold on a proxy of a Lease that `bind()` bound a Session to it for.

        Args:
            session: The Session's id.
            proxy: The proxy the Lease was bound to.
        """
        ...

    async def unbind(self, session: str) -> None:
        """Release the binding of a Session, such as one Retired, so it no longer counts toward its proxy's Sessions.

        It still counts toward the proxy until its Leases that went through it end.

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
        """Return how long the proxies listed, which no Session holds as its own, are all cooling down for.

        Returns:
            Seconds until one is available; 0 if one is, and `math.inf` if none ever
            will be, as every one listed is Retired, or none is.
        """
        ...
