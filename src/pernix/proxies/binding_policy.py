"""Binding policies: which proxy a Session goes through, picked among those the Proxy Pool lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pernix.proxies.proxy_url import proxy_address

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.account import Account
    from pernix.proxies.proxy import Proxy
    from pernix.proxies.proxy_pool import ProxyCandidate, ProxyHealth
    from pernix.settings import ProxySettings

__all__ = ["Binding", "BindingPolicy", "BindingReason", "DefaultBindingPolicy"]

type BindingReason = Literal["new", "lease", "cooldown", "retired", "expired", "unlisted"]
"""Why a Session is being bound: it has no proxy yet; a Lease starts while its proxy is available; or its proxy cools
down, is Retired, expired, or is no longer listed."""

_FOLLOWED: Mapping[str, frozenset[BindingReason]] = {
    "unavailable": frozenset(),
    "every_lease": frozenset(),
    "retired": frozenset({"lease", "cooldown"}),
    "never": frozenset({"lease", "cooldown", "retired", "expired", "unlisted"}),
}
"""For each `proxies.rebind`, the reasons for which the default policy keeps a Session on its proxy, whatever its
health; the others keep it only while it is open."""


@dataclass(frozen=True, slots=True)
class Binding:
    """What a binding policy knows of a Session being bound to a proxy, before a Lease's first fetch through it.

    Its `repr` shows its proxies without their credentials.

    Attributes:
        session: The Session's id.
        account: The Account the Session logs in with, if any.
        current: The proxy the Session is bound to, if any.
        health: The health of `current`; `None` without one, or when the pool no
            longer lists it.
        reason: Why the Session is being bound.
        candidates: Every proxy the pool lists, whatever its health, and the
            Session's own, if it holds one, least recently used first.
        max_sessions: Sessions a proxy is bound to at most; `None` for no limit.
    """

    session: str
    account: Account | None = None
    current: str | None = None
    health: ProxyHealth | None = None
    reason: BindingReason = "new"
    candidates: tuple[ProxyCandidate, ...] = ()
    max_sessions: int | None = None

    @property
    def open(self) -> tuple[ProxyCandidate, ...]:
        """The candidates the Session can be bound to: available, with room for it, least recently used first.

        A candidate has room while fewer than `max_sessions` other Sessions occupy it.
        """
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.health == "available"
            and (self.max_sessions is None or len(candidate.occupants - {self.session}) < self.max_sessions)
        )

    def __repr__(self) -> str:
        current = None if self.current is None else proxy_address(self.current)
        return (
            f"Binding({self.session!r}, reason={self.reason!r}, current={current!r}, candidates={len(self.candidates)})"
        )


class BindingPolicy(Protocol):
    """Picks the proxy a Session goes through; replace the default with `proxies.binding`.

    A policy is asked before the first fetch of each Lease that goes through the
    Session's proxy, and once more when a fetch through it failed. It returns:

    - `binding.current` to keep the Session on its proxy, whatever its health: while
      the proxy cools down, the Session cools down with it, and its Requests wait
      for it; once the proxy is Retired, expired, or no longer listed, the Session
      is Retired with it;
    - another candidate's URL to bind that;
    - a `pernix.proxies.Proxy` record to give the Session a proxy of its own, which
      the pool does not list, such as its Account's, or a sticky session on a
      gateway built from its id: it is held for that Session alone, takes no
      turns, and refreshes keep it; it is checked, its password masked in the
      Run's log, and its health kept as any other's, and it is among the
      Session's candidates from then on, until its `expires`, which a record
      given again renews;
    - or `None` to have the Session's Requests wait, as when none fits.

    The pool binds a Session only to a proxy available with room for it; should
    another Session take the last room first, the policy is asked again, with the
    candidates read anew, a few times. A policy reads the candidates but never
    changes them, and has no side effects, as it may be asked again for one Lease.
    One that raises, or returns a URL the pool does not list, fails the fetch.

    Example:
        >>> class ByRegion:
        ...     async def bind(self, binding: Binding) -> str | Proxy | None:
        ...         if binding.reason == "lease":
        ...             return binding.current  # keep it while it is available
        ...         region = REGIONS.get(binding.account.id if binding.account else "", "us")
        ...         return next((proxy.url for proxy in binding.open if region in proxy.tags), None)
    """

    async def bind(self, binding: Binding) -> str | Proxy | None:
        """Pick the proxy a Session goes through.

        Args:
            binding: The Session, its proxy, and the candidates.

        Returns:
            The URL of the proxy to bind, `binding.current` to keep it, a `Proxy` of
            the Session's own, or `None` to wait.
        """
        ...


class DefaultBindingPolicy:
    """Keeps a Session on its proxy as `proxies.rebind` says, or binds it to the open one with the fewest Sessions.

    By `proxies.rebind`, a Session is bound to another proxy:

    - `unavailable`: once its proxy cools down, is Retired, expired, or is no longer
      listed;
    - `every_lease`: at each Lease, if another proxy is open, so its Leases go
      through the proxies in turn;
    - `retired`: only once its proxy is Retired, expired, or no longer listed; while
      it cools down, the Session cools down with it;
    - `never`: the Session cools down with its proxy, and is Retired with it.

    Among the open proxies with the fewest other Sessions occupying them, the least
    recently used is bound, so Sessions bound while enough proxies are available get
    one each. With none open, the Session waits.
    """

    def __init__(self, settings: ProxySettings) -> None:
        """Configure the policy.

        Args:
            settings: The `proxies.*` settings.
        """
        self._rebind = settings.rebind
        self._followed = _FOLLOWED[settings.rebind]

    async def bind(self, binding: Binding) -> str | None:
        """Pick the proxy a Session goes through; see the class description.

        Args:
            binding: The Session, its proxy, and the candidates.

        Returns:
            The proxy to bind, or `None` to wait.
        """
        if binding.current is not None and binding.reason in self._followed:
            return binding.current  # it waits for its proxy, or is Retired with it
        open_ = binding.open
        choices = [candidate for candidate in open_ if candidate.url != binding.current]
        if len(choices) < len(open_) and (self._rebind != "every_lease" or not choices):
            return binding.current  # it is open
        fewest = min(choices, key=lambda candidate: len(candidate.occupants - {binding.session}), default=None)
        return None if fewest is None else fewest.url
