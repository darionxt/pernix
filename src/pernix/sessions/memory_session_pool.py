"""A Session Pool kept in this process, for one Run."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pernix.cookie_jar import Cookie, CookieKey, cookie_key
from pernix.errors import SessionError
from pernix.session import Session, SessionState
from pernix.sessions.session_pool import SessionLease

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from pernix.sessions.session_pool import Outcome, StateDelta
    from pernix.stats import Stats

__all__ = ["MemorySessionPool", "SessionHealth"]

type SessionHealth = Literal["available", "cooldown", "retired"]
"""Whether a Session can be leased: available, cooling down until a time, or retired for good."""


@dataclass(slots=True)
class _Entry:
    """A Session as the pool holds it: its state, cookies by key, and its health and Leases."""

    id: str
    cookies: dict[CookieKey, Cookie]
    local_storage: dict[str, dict[str, str]]
    proxy: str | None
    profile: str | None
    account: str | None
    tags: frozenset[str]
    max_concurrency: int
    removed: dict[CookieKey, float] = field(default_factory=dict[CookieKey, float])
    """When the cookies removed were removed, so an older change cannot bring one back."""
    leases: dict[str, float] = field(default_factory=dict[str, float])
    """When each Lease expires, by token."""
    cool_until: float = 0.0
    retired: bool = False
    last_leased: float = 0.0

    def state(self) -> SessionState:
        return SessionState(
            cookies=list(self.cookies.values()),
            local_storage={origin: dict(items) for origin, items in self.local_storage.items()},
            proxy=self.proxy,
            profile=self.profile,
            account=self.account,
        )

    def merge(self, delta: StateDelta) -> None:
        """Apply the changes of a fetch, cookie by cookie, unless the pool holds a later change."""
        for cookie in delta.cookies:
            key = cookie_key(cookie)
            held = self.cookies.get(key)
            if (held is None or held.modified <= cookie.modified) and self.removed.get(key, -1.0) < cookie.modified:
                self.cookies[key] = cookie
                self.removed.pop(key, None)
        for removal in delta.removed:
            key = (removal.domain, removal.host_only, removal.path, removal.name)
            held = self.cookies.get(key)
            if held is None or held.modified <= removal.removed:
                self.cookies.pop(key, None)
                self.removed[key] = max(self.removed.get(key, removal.removed), removal.removed)
        for origin, items in delta.local_storage.items():
            if items:
                self.local_storage[origin] = dict(items)
            else:
                self.local_storage.pop(origin, None)


class MemorySessionPool:
    """A Session Pool kept in this process, for one Run.

    Sessions are created as Requests need them, when none is free, up to `size`; a
    Request naming a Session the pool does not hold yet has it created under that
    id, while there is room. `eager` creates them all at once. Each Session takes up
    to `max_concurrency` Leases at once, and the least recently leased of the free
    ones is leased first. A `rate_limited` outcome cools its Session down until the
    time it gives; the other outcomes leave its health as it is. A Lease expires
    after `lease_ttl` seconds unless renewed, and a release or a save from one that
    expired or was released is rejected. States are merged per cookie, by
    modification time.

    Attributes:
        name: The pool's name.
    """

    def __init__(
        self,
        name: str = "default",
        *,
        size: int = 8,
        max_concurrency: int = 1,
        lease_ttl: float = 300.0,
        eager: bool = False,
        stats: Stats | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Create the pool.

        Args:
            name: The pool's name.
            size: Sessions the pool holds at most.
            max_concurrency: Leases each Session takes at once, unless `add()` gives
                its own.
            lease_ttl: Seconds a Lease lasts unless renewed.
            eager: Create all `size` Sessions now.
            stats: Where created Sessions are counted, as `sessions.created`.
            clock: The current time, in seconds since the epoch.
        """
        self.name = name
        self._size = size
        self._max_concurrency = max_concurrency
        self._lease_ttl = lease_ttl
        self._stats = stats
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._created = 0
        self._changed = asyncio.Event()
        if eager:
            while len(self._entries) < size:
                self._create(None)

    def add(self, session: Session, *, tags: Collection[str] = (), max_concurrency: int | None = None) -> None:
        """Hold a Session made beforehand, such as one bound to an Account.

        Args:
            session: The Session.
            tags: Tags that `acquire(tags=...)` selects it by.
            max_concurrency: Leases it takes at once; the pool's own when `None`.

        Raises:
            ValueError: The pool holds a Session of that id already, or is full.
        """
        if session.id in self._entries:
            raise ValueError(f"the Session Pool {self.name!r} holds a Session {session.id!r} already")
        if len(self._entries) >= self._size:
            raise ValueError(f"the Session Pool {self.name!r} is full, with {self._size} Sessions")
        self._hold(session, frozenset(tags), max_concurrency or self._max_concurrency)

    def health(self, session: str) -> SessionHealth | None:
        """Return the health of a Session.

        Args:
            session: The Session's id.

        Returns:
            Its health, or `None` if the pool does not hold it.
        """
        entry = self._entries.get(session)
        if entry is None:
            return None
        if entry.retired:
            return "retired"
        return "cooldown" if entry.cool_until > self._clock() else "available"

    def retire(self, session: str) -> None:
        """Take a Session out of use for good.

        Args:
            session: The Session's id.

        Raises:
            KeyError: The pool does not hold it.
        """
        self._entries[session].retired = True
        self._notify()

    def __len__(self) -> int:
        return len(self._entries)

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
            pernix.errors.SessionError: No Session can ever be leased.
        """
        wanted = frozenset(tags)
        loop = asyncio.get_running_loop()
        end = None if timeout is None else loop.time() + timeout
        while True:
            now = self._clock()
            entry = self._leasable(session, wanted, now)
            if entry is not None:
                return self._lease(entry, now)
            left = None if end is None else end - loop.time()
            if left is not None and left <= 0:
                return None
            await self._wait(_soonest(left, self._next_change(now)))

    async def ready_in(self, *, session: str | None = None, tags: Collection[str] = ()) -> float:
        """Return how long the Sessions that fit are all cooling down for.

        Args:
            session: The id of the Session; any Session when `None`.
            tags: Tags the Session must have, when `session` is `None`.

        Returns:
            Seconds until one that fits stops cooling down; 0 if one is not cooling
            down, or can be created.

        Raises:
            pernix.errors.SessionError: No Session can ever be leased.
        """
        now = self._clock()
        entries = self._fitting(session, frozenset(tags))
        if not entries:
            return 0.0  # it can be created
        return max(0.0, min(entry.cool_until for entry in entries) - now)

    async def renew(self, lease: SessionLease) -> bool:
        """Extend a Lease by `lease_ttl`.

        Args:
            lease: The Lease.

        Returns:
            `False` if the Lease expired or was released.
        """
        entry = self._held_by(lease)
        if entry is None:
            return False
        entry.leases[lease.token] = self._clock() + self._lease_ttl
        return True

    async def release(self, lease: SessionLease, outcome: Outcome) -> bool:
        """End a Lease, cooling its Session down for a `rate_limited` outcome.

        Args:
            lease: The Lease.
            outcome: How the Session fared.

        Returns:
            `False`, changing nothing, if the Lease expired or was released.
        """
        entry = self._held_by(lease)
        if entry is None:
            return False
        del entry.leases[lease.token]
        if outcome.kind == "rate_limited" and outcome.until is not None:
            entry.cool_until = max(entry.cool_until, outcome.until)
        self._notify()
        return True

    async def load_state(self, lease: SessionLease) -> SessionState:
        """Return the state of a leased Session.

        Args:
            lease: The Lease.

        Returns:
            A copy of the state.

        Raises:
            pernix.errors.SessionError: The Lease expired or was released.
        """
        entry = self._held_by(lease)
        if entry is None:
            raise SessionError(f"the Lease on the Session {lease.session!r} expired or was released")
        return entry.state()

    async def save_state(self, lease: SessionLease, delta: StateDelta) -> bool:
        """Merge the changes a fetch made into the state of a leased Session, cookie by cookie.

        Args:
            lease: The Lease.
            delta: The changes.

        Returns:
            `False`, changing nothing, if the Lease expired or was released.
        """
        entry = self._held_by(lease)
        if entry is None:
            return False
        entry.merge(delta)
        return True

    def _leasable(self, session: str | None, tags: frozenset[str], now: float) -> _Entry | None:
        """The Session to lease now, created if needed; `None` if every one that fits is busy or cooling down."""
        self._expire(now)
        if session is not None:
            entry = self._entries.get(session) or self._create(session)
            if entry.retired:
                raise SessionError(f"the Session {session!r} of the Session Pool {self.name!r} is retired")
            return entry if _free(entry, now) else None
        entries = self._fitting(None, tags)
        free = [entry for entry in entries if _free(entry, now)]
        if free:
            return min(free, key=lambda entry: entry.last_leased)
        if len(self._entries) < self._size and not tags:
            return self._create(None)
        return None

    def _fitting(self, session: str | None, tags: frozenset[str]) -> list[_Entry]:
        """The Sessions that fit and are not retired; empty when one is still to be created.

        Raises:
            pernix.errors.SessionError: None ever will fit.
        """
        if session is not None:
            entry = self._entries.get(session)
            if entry is None:
                self._check_room(session)
                return []
            if entry.retired:
                raise SessionError(f"the Session {session!r} of the Session Pool {self.name!r} is retired")
            return [entry]
        entries = [entry for entry in self._entries.values() if not entry.retired and tags <= entry.tags]
        if not entries and (tags or len(self._entries) >= self._size):
            wanted = f" with the tags {sorted(tags)}" if tags else ""
            raise SessionError(f"the Session Pool {self.name!r} has no Session{wanted} that is not retired")
        return entries

    def _check_room(self, session: str | None) -> None:
        if len(self._entries) >= self._size:
            named = "" if session is None else f", and none is {session!r}"
            raise SessionError(f"the Session Pool {self.name!r} holds {self._size} Sessions already{named}")

    def _create(self, session: str | None) -> _Entry:
        self._check_room(session)
        self._created += 1
        if session is None:
            session = f"{self.name}-{self._created}"
            while session in self._entries:  # an id a Request named
                session = f"{self.name}-{uuid.uuid4().hex[:8]}"
        entry = self._hold(Session(session), frozenset(), self._max_concurrency)
        if self._stats is not None:
            self._stats.inc("sessions.created")
        return entry

    def _hold(self, session: Session, tags: frozenset[str], max_concurrency: int) -> _Entry:
        entry = _Entry(
            id=session.id,
            cookies={cookie_key(cookie): cookie for cookie in session.cookies},
            local_storage={origin: dict(items) for origin, items in session.local_storage.items()},
            proxy=session.proxy,
            profile=session.profile,
            account=session.account,
            tags=tags,
            max_concurrency=max_concurrency,
        )
        self._entries[session.id] = entry
        self._notify()
        return entry

    def _lease(self, entry: _Entry, now: float) -> SessionLease:
        token = uuid.uuid4().hex
        expires_at = now + self._lease_ttl
        entry.leases[token] = expires_at
        entry.last_leased = now
        return SessionLease(entry.id, token, expires_at)

    def _held_by(self, lease: SessionLease) -> _Entry | None:
        """The Session a Lease holds, if the Lease is still valid."""
        self._expire(self._clock())
        entry = self._entries.get(lease.session)
        return entry if entry is not None and lease.token in entry.leases else None

    def _expire(self, now: float) -> None:
        for entry in self._entries.values():
            for token in [token for token, expires_at in entry.leases.items() if expires_at <= now]:
                del entry.leases[token]

    def _next_change(self, now: float) -> float | None:
        """Seconds until a cooldown ends or a Lease expires, which could free a Session."""
        times = [entry.cool_until for entry in self._entries.values() if entry.cool_until > now]
        times += [expires_at for entry in self._entries.values() for expires_at in entry.leases.values()]
        return max(0.0, min(times) - now) if times else None

    async def _wait(self, at_most: float | None) -> None:
        changed = self._changed
        try:
            async with asyncio.timeout(at_most):
                await changed.wait()
        except TimeoutError:
            pass

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


def _free(entry: _Entry, now: float) -> bool:
    return not entry.retired and entry.cool_until <= now and len(entry.leases) < entry.max_concurrency


def _soonest(*delays: float | None) -> float | None:
    given = [delay for delay in delays if delay is not None]
    return min(given) if given else None
