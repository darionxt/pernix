"""Binding policies: which proxy a Session goes through, picked among those the Proxy Pool lists."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from pernix.proxies.proxy_url import proxy_address

if TYPE_CHECKING:
    from pernix.account import Account
    from pernix.proxies.proxy_pool import ProxyCandidate, ProxyHealth
    from pernix.settings import ProxySettings

__all__ = ["Binding", "BindingPolicy", "BindingReason", "DefaultBindingPolicy"]

type BindingReason = Literal["new", "lease", "cooldown", "retired", "unlisted"]
"""Why a Session is being bound: it has no proxy yet; a Lease starts while its proxy is available; or its proxy cools
down, is Retired, or is no longer listed."""


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
        candidates: Every proxy the pool lists, whatever its health, least recently
            used first.
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
        """The candidates the Session can be bound to: available, with room for it, least recently used first."""
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.health == "available"
            and (self.max_sessions is None or len(candidate.sessions - {self.session}) < self.max_sessions)
        )

    def __repr__(self) -> str:
        current = None if self.current is None else proxy_address(self.current)
        return (
            f"Binding({self.session!r}, reason={self.reason!r}, current={current!r}, candidates={len(self.candidates)})"
        )


class BindingPolicy(Protocol):
    """Picks the proxy a Session goes through; replace the default with `proxies.binding`.

    A policy is asked before the first fetch of each Lease that goes through the
    Session's proxy, while some proxy is available. It returns the Session's
    current proxy to keep it, another candidate's URL to bind that, or `None` to
    have the Session's Requests wait, as when none fits. The pool binds a Session
    only to a proxy available with room for it; should another Session take the
    last room first, the policy is asked again, with the candidates read anew, a
    few times. A policy reads the candidates but never changes them, and has no
    side effects, as it may be asked again for one Lease. One that raises, or
    returns a proxy the pool does not list, fails the fetch.

    Example:
        >>> class ByRegion:
        ...     async def bind(self, binding: Binding) -> str | None:
        ...         if binding.reason == "lease":
        ...             return binding.current  # keep it while it is available
        ...         region = REGIONS.get(binding.account.id if binding.account else "", "us")
        ...         return next((proxy.url for proxy in binding.open if region in proxy.tags), None)
    """

    async def bind(self, binding: Binding) -> str | None:
        """Pick the proxy a Session goes through.

        Args:
            binding: The Session, its proxy, and the candidates.

        Returns:
            The URL of the proxy to bind, `binding.current` to keep it, or `None` to
            wait.
        """
        ...


class DefaultBindingPolicy:
    """Keeps a Session on its proxy while that is available, and binds it to the open one with the fewest Sessions.

    With `proxies.rebind` set to `every_lease`, each Lease binds the Session to
    another open proxy, if there is one, so its Leases go through the proxies in
    turn. Among the open proxies with the fewest other Sessions, the least recently
    used is bound, so Sessions bound while enough proxies are available get one
    each. With none open, the Session waits.
    """

    def __init__(self, settings: ProxySettings) -> None:
        """Configure the policy.

        Args:
            settings: The `proxies.*` settings.
        """
        self._rebind = settings.rebind

    async def bind(self, binding: Binding) -> str | None:
        """Pick the proxy a Session goes through; see the class description.

        Args:
            binding: The Session, its proxy, and the candidates.

        Returns:
            The proxy to bind, or `None` to wait.
        """
        open_ = binding.open
        choices = [candidate for candidate in open_ if candidate.url != binding.current]
        if len(choices) < len(open_) and (self._rebind == "unavailable" or not choices):
            return binding.current  # it is open
        fewest = min(choices, key=lambda candidate: len(candidate.sessions - {binding.session}), default=None)
        return None if fewest is None else fewest.url
