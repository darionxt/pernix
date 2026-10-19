"""A Session Pool kept in this process, for one Run."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import msgspec

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
class _Claim:
    """A Lease given on a Session."""

    expires_at: float
    login: int
    """The Session's logins when the Lease was given, or when its holder logged the Session in."""
    logged_in: bool = False
    """Whether its holder logged the Session in."""


@dataclass(slots=True)
class _Entry:
    """A Session as the pool holds it: its state, cookies by key, and its health, login, and Leases."""

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
    leases: dict[str, _Claim] = field(default_factory=dict[str, _Claim])
    """The Leases given on the Session, by token."""
    cool_until: float = 0.0
    retired: bool = False
    last_leased: float = 0.0
    logged_in: bool = False
    logging_in: str | None = None
    """The token of the Lease logging the Session in, if any."""
    logins: int = 0
    login_failures: int = 0
    """Failed logins since the last that worked."""
    blocks: int = 0
    """Blocked Responses in a row."""

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
        if delta.proxy is not msgspec.UNSET:
            self.proxy = delta.proxy


class MemorySessionPool:
    """A Session Pool kept in this process, for one Run.

    Sessions are created as Requests need them, when none is free, up to `size`; a
    Request naming a Session the pool does not hold yet has it created under that
    id, while there is room. `eager` creates them all at once. Each Session takes up
    to `max_concurrency` Leases at once, and the least recently leased of the free
    ones is leased first. A `rate_limited` outcome cools its Session down until the
    time it gives. A Lease expires after `lease_ttl` seconds unless renewed, and a
    release or a save from one that expired or was released is rejected. States are
    merged per cookie, by modification time.

    An outcome with an `until`, such as `rate_limited`, cools its Session down
    until then, and `proxy_retired` Retires it. A `blocked` outcome counts toward the
    Session's Retirement, after `max_blocks` in a row; an `ok` one ends the row. Each
    Session Retired is counted as `sessions.retirements`, whatever the reason.

    With `logs_in`, a Session logs in before it is first used: its first Lease must
    log it in, and no other is given on it until the holder reports `logged_in()`.
    An `auth_expired` outcome has it log in again. A `login_failed` outcome cools it
    down until the time it gives, then has it log in again, and Retires it after
    `login_attempts` in a row; a login counts as having worked once the Lease that
    made it is released with another outcome. Either changes nothing if the Session
    logged in again since the Lease was given, or since its holder logged it in, as
    it tells of a login replaced since. The other outcomes leave a Session as it is.

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
        logs_in: bool = False,
        login_attempts: int = 3,
        max_blocks: int = 3,
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
            logs_in: Whether Sessions log in before they are used.
            login_attempts: Failed logins in a row after which a Session is Retired.
            max_blocks: Blocked outcomes in a row after which a Session is Retired.
            stats: Where created and Retired Sessions are counted, as
                `sessions.created` and `sessions.retirements`.
            clock: The current time, in seconds since the epoch.
        """
        self.name = name
        self._size = size
        self._max_concurrency = max_concurrency
        self._lease_ttl = lease_ttl
        self._logs_in = logs_in
        self._login_attempts = login_attempts
        self._max_blocks = max_blocks
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
        self._retire(self._entries[session])
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
        """Lease a Session that is neither cooling down, retired, logging in, nor at its `max_concurrency`.

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
        entry.leases[lease.token].expires_at = self._clock() + self._lease_ttl
        return True

    async def logged_in(self, lease: SessionLease) -> bool:
        """Mark the Session of a Lease that was to log it in as logged in, so other Leases may be given on it.

        Args:
            lease: The Lease.

        Returns:
            `False`, changing nothing, if the Lease expired or was released, or was
            not to log its Session in.
        """
        entry = self._held_by(lease)
        if entry is None or entry.logging_in != lease.token:
            return False
        entry.logging_in = None
        entry.logged_in = True
        entry.logins += 1
        claim = entry.leases[lease.token]
        claim.login, claim.logged_in = entry.logins, True
        self._notify()
        return True

    async def release(self, lease: SessionLease, outcome: Outcome) -> bool:
        """End a Lease, cooling its Session down, having it log in again, or Retiring it, as its outcome says.

        Args:
            lease: The Lease.
            outcome: How the Session fared.

        Returns:
            `False`, changing nothing, if the Lease expired or was released.
        """
        entry = self._held_by(lease)
        if entry is None:
            return False
        claim = entry.leases.pop(lease.token)
        if entry.logging_in == lease.token:
            entry.logging_in = None  # the login was given up; another Lease logs the Session in
        logging_out = outcome.kind in ("auth_expired", "login_failed")
        replaced = claim.login != entry.logins  # the outcome tells of a login the Session replaced since
        if outcome.until is not None and not (logging_out and replaced):
            entry.cool_until = max(entry.cool_until, outcome.until)
        if logging_out:
            if not replaced:
                self._logged_out(entry, outcome)
        elif claim.logged_in:
            entry.login_failures = 0  # its fetch, after the login, went on logged in
        if outcome.kind == "proxy_retired":
            self._retire(entry)
        elif outcome.kind == "blocked":
            entry.blocks += 1
            if entry.blocks >= self._max_blocks:
                self._retire(entry)
        elif outcome.kind == "ok":
            entry.blocks = 0
        self._notify()
        return True

    def _logged_out(self, entry: _Entry, outcome: Outcome) -> None:
        """Have a Session log in again, and Retire it after too many failed logins in a row."""
        entry.logged_in = False
        if outcome.kind != "login_failed":
            return
        entry.login_failures += 1
        if entry.login_failures >= self._login_attempts:
            self._retire(entry)

    def _retire(self, entry: _Entry) -> None:
        """Take a Session out of use for good, counting it once."""
        if entry.retired:
            return
        entry.retired = True
        if self._stats is not None:
            self._stats.inc("sessions.retirements")

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
        entry.leases[token] = _Claim(expires_at, entry.logins)
        entry.last_leased = now
        must_log_in = self._logs_in and not entry.logged_in
        if must_log_in:
            entry.logging_in = token
        return SessionLease(entry.id, token, expires_at, must_log_in=must_log_in)

    def _held_by(self, lease: SessionLease) -> _Entry | None:
        """The Session a Lease holds, if the Lease is still valid."""
        self._expire(self._clock())
        entry = self._entries.get(lease.session)
        return entry if entry is not None and lease.token in entry.leases else None

    def _expire(self, now: float) -> None:
        for entry in self._entries.values():
            for token in [token for token, claim in entry.leases.items() if claim.expires_at <= now]:
                del entry.leases[token]
                if entry.logging_in == token:
                    entry.logging_in = None

    def _next_change(self, now: float) -> float | None:
        """Seconds until a cooldown ends or a Lease expires, which could free a Session."""
        times = [entry.cool_until for entry in self._entries.values() if entry.cool_until > now]
        times += [claim.expires_at for entry in self._entries.values() for claim in entry.leases.values()]
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
    """Whether a Session can be leased now: neither retired, cooling down, logging in, nor at its `max_concurrency`."""
    return (
        not entry.retired
        and entry.cool_until <= now
        and entry.logging_in is None
        and len(entry.leases) < entry.max_concurrency
    )


def _soonest(*delays: float | None) -> float | None:
    given = [delay for delay in delays if delay is not None]
    return min(given) if given else None
