"""The Proxy Pool protocol: the proxies Requests are sent through, and their health."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection

__all__ = ["ProxyCounts", "ProxyHealth", "ProxyPool"]

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


class ProxyPool(Protocol):
    """The proxies Requests are sent through, with their health.

    The proxies are those last listed by the sources; a proxy no longer listed is
    not used, but its health is kept, should it be listed again. A proxy that fails
    cools down, longer with each failure in a row, and is Retired after too many.
    Proxies are URLs as `pernix.proxies.proxy_url.proxy_url()` gives them. The
    in-process implementation is `pernix.proxies.MemoryProxyPool`; one shared by
    several processes keeps the same protocol.
    """

    async def replace(self, proxies: Collection[str]) -> None:
        """List the proxies the sources gave, in place of those listed before.

        Args:
            proxies: The proxies.
        """
        ...

    async def take(self) -> str | None:
        """Take a proxy for one fetch: the available one used least recently.

        Returns:
            The proxy, or `None` if none is available now.
        """
        ...

    async def bind(self, session: str, current: str | None) -> str | None:
        """Return the proxy a Session goes through: its own while available, else the one with the fewest Sessions.

        The pool holds the binding of each Session, so that Leases of one Session
        that overlap are given the same proxy, though each loaded its state before
        the other saved it.

        Args:
            session: The Session's id.
            current: The proxy the Session's state names, for a Session the pool
                bound to none yet; the pool's binding goes first.

        Returns:
            The proxy, or `None` if none is available now.
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
