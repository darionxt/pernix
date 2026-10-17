"""The Session Pool protocol: leasing Sessions, reporting how they fared, and keeping their state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Self

import msgspec

from pernix.cookie_jar import Cookie, cookie_key

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from pernix.cookie_jar import CookieKey
    from pernix.session import SessionState

__all__ = ["CookieRemoval", "Outcome", "OutcomeKind", "SessionLease", "SessionPool", "StateDelta"]

type OutcomeKind = Literal["ok", "rate_limited", "blocked", "auth_expired", "proxy_failed", "error"]
"""How a Session fared in a fetch."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """How a Session fared in the fetch it was leased for.

    Attributes:
        kind: `ok` when nothing went wrong; `rate_limited` when the site asked the
            Session to slow down, until `until`; `blocked` when the site refused
            the Session; `auth_expired` when its login expired; `proxy_failed`
            when its proxy failed; and `error` when the fetch failed otherwise.
        until: For `rate_limited`, when the Session may be used again, in seconds
            since the epoch.
    """

    kind: OutcomeKind
    until: float | None = None


@dataclass(frozen=True, slots=True)
class SessionLease:
    """A time-limited claim on a Session.

    Attributes:
        session: The Session's id.
        token: Tells this Lease from the others; the pool rejects a release or a
            save from a Lease that expired or was released.
        expires_at: When the Lease ends unless renewed, in seconds since the epoch.
    """

    session: str
    token: str
    expires_at: float


class CookieRemoval(msgspec.Struct, frozen=True, kw_only=True):
    """A cookie a fetch removed, named as a cookie jar tells cookies apart.

    Attributes:
        name: The cookie name.
        domain: Its domain.
        host_only: Its host-only flag.
        path: Its path.
        removed: When it was removed, in seconds since the epoch.
    """

    name: str
    domain: str
    host_only: bool
    path: str
    removed: float


class StateDelta(msgspec.Struct, frozen=True, kw_only=True):
    """The changes one fetch made to the state of a Session, which the pool merges per cookie.

    Attributes:
        cookies: The cookies set or replaced.
        removed: The cookies removed.
        local_storage: The origins whose localStorage changed, each with all of it;
            an empty one for an origin cleared.
    """

    cookies: list[Cookie] = msgspec.field(default_factory=list[Cookie])
    removed: list[CookieRemoval] = msgspec.field(default_factory=list[CookieRemoval])
    local_storage: dict[str, dict[str, str]] = msgspec.field(default_factory=dict[str, dict[str, str]])

    @classmethod
    def between(
        cls,
        before: SessionState,
        after: SessionState,
        *,
        now: float,
        removed_at: Mapping[CookieKey, float] | None = None,
    ) -> Self:
        """Return the changes that turned one state of a Session into another.

        Args:
            before: The state the fetch started with.
            after: The state it ended with.
            now: When the changes are recorded, in seconds since the epoch: the time
                of the cookies removed that `removed_at` does not time.
            removed_at: When cookies were removed, by key, such as the `removals`
                of the Session's cookie jar.

        Returns:
            The delta; empty when nothing changed.
        """
        removed_at = removed_at or {}
        old = {cookie_key(cookie): cookie for cookie in before.cookies}
        new = {cookie_key(cookie): cookie for cookie in after.cookies}
        storage = {
            origin: items for origin, items in after.local_storage.items() if before.local_storage.get(origin) != items
        }
        storage.update({origin: {} for origin in before.local_storage if origin not in after.local_storage})
        return cls(
            cookies=[cookie for key, cookie in new.items() if old.get(key) != cookie],
            removed=[
                CookieRemoval(
                    name=key[3], domain=key[0], host_only=key[1], path=key[2], removed=removed_at.get(key, now)
                )
                for key in old
                if key not in new
            ],
            local_storage=storage,
        )

    @property
    def empty(self) -> bool:
        """Whether the delta changes nothing."""
        return not (self.cookies or self.removed or self.local_storage)


class SessionPool(Protocol):
    """A named set of Sessions that Requests lease, one fetch at a time.

    A Lease holds a Session for one fetch, and its state is loaded when the fetch
    starts and saved, as the changes the fetch made, when it ends. Releasing the
    Lease reports how the Session fared, which may cool it down. The in-process
    implementation is `pernix.sessions.MemorySessionPool`; one shared by several
    processes keeps the same protocol.

    Attributes:
        name: The pool's name.
    """

    name: str

    async def acquire(
        self,
        *,
        session: str | None = None,
        tags: Collection[str] = (),
        timeout: float | None = None,  # noqa: ASYNC109  # a pool shared by processes waits on its own terms
    ) -> SessionLease | None:
        """Lease a Session that is neither cooling down, retired, nor at its `max_concurrency`.

        Args:
            session: The id of the Session to lease; any Session when `None`.
            tags: Tags the Session must have, when `session` is `None`.
            timeout: Seconds to wait for one; `None` to wait as long as it takes.

        Returns:
            The Lease, or `None` if no Session could be leased in time.

        Raises:
            pernix.errors.SessionError: No Session can ever be leased: the one named
                is retired, or the pool cannot create it, or every Session that
                fits is retired.
        """
        ...

    async def ready_in(self, *, session: str | None = None, tags: Collection[str] = ()) -> float:
        """Return how long the Sessions that fit are all cooling down for.

        Args:
            session: The id of the Session; any Session when `None`.
            tags: Tags the Session must have, when `session` is `None`.

        Returns:
            Seconds until one that fits stops cooling down; 0 if one is not cooling
            down, or can be created.

        Raises:
            pernix.errors.SessionError: No Session can ever be leased, as for
                `acquire()`.
        """
        ...

    async def renew(self, lease: SessionLease) -> bool:
        """Extend a Lease by the pool's lease time.

        Args:
            lease: The Lease.

        Returns:
            `False` if the Lease expired or was released.
        """
        ...

    async def release(self, lease: SessionLease, outcome: Outcome) -> bool:
        """End a Lease, reporting how its Session fared.

        Args:
            lease: The Lease.
            outcome: How the Session fared.

        Returns:
            `False`, changing nothing, if the Lease expired or was released.
        """
        ...

    async def load_state(self, lease: SessionLease) -> SessionState:
        """Return the state of a leased Session.

        Args:
            lease: The Lease.

        Returns:
            A copy of the state.

        Raises:
            pernix.errors.SessionError: The Lease expired or was released.
        """
        ...

    async def save_state(self, lease: SessionLease, delta: StateDelta) -> bool:
        """Merge the changes a fetch made into the state of a leased Session.

        Cookies are merged one by one: a cookie set, or removed, replaces the one the
        pool holds unless that was modified later. localStorage is replaced per
        origin.

        Args:
            lease: The Lease.
            delta: The changes.

        Returns:
            `False`, changing nothing, if the Lease expired or was released.
        """
        ...
