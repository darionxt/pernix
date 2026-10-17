"""Sessions: the identity Requests are sent under, with its cookies, browser storage, proxy, and Profile."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Self

import msgspec

from pernix.cookie_jar import Cookie, CookieJar
from pernix.logs.redaction import MASK

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["Session", "SessionState"]


class SessionState(msgspec.Struct, kw_only=True):
    """The state of a Session, as stored and moved between processes.

    Attributes:
        cookies: The cookies, in the order they were first set.
        local_storage: The browser's localStorage, by origin, then by key.
        proxy: The proxy bound to the Session, if any.
        profile: The Profile bound to the Session, if any.
        account: The Account the Session logs in with, if any.
    """

    cookies: list[Cookie] = msgspec.field(default_factory=list[Cookie])
    local_storage: dict[str, dict[str, str]] = msgspec.field(default_factory=dict[str, dict[str, str]])
    proxy: str | None = None
    profile: str | None = None
    account: str | None = None

    def __repr__(self) -> str:
        return (
            f"SessionState({_described(len(self.cookies), self.local_storage, self.proxy, self.profile, self.account)})"
        )


class Session:
    """The identity a Request is sent under: its cookies and browser storage, a proxy, a Profile, and an Account.

    The HTTP Fetcher sends a Request under a Session with the Session's cookies that
    match each redirect hop, together with the Request's own `cookies`, and keeps the
    cookies set during the fetch, while the Request's own are not kept. The Session's
    proxy and Profile apply unless the Request names its own. Cookie values and
    storage never appear in a `repr`.

    Attributes:
        id: The Session's identifier, as `Request.session` and `Response.session`
            name it.
        cookies: The cookies.
        local_storage: The browser's localStorage, by origin, then by key.
        proxy: The proxy bound to the Session, if any.
        profile: The Profile bound to the Session, if any.
        account: The Account the Session logs in with, if any.
    """

    def __init__(
        self,
        id: str | None = None,
        *,
        cookies: Iterable[Cookie] = (),
        local_storage: Mapping[str, Mapping[str, str]] | None = None,
        proxy: str | None = None,
        profile: str | None = None,
        account: str | None = None,
    ) -> None:
        """Create a Session.

        Args:
            id: The identifier; a random one when `None`.
            cookies: The cookies to start with.
            local_storage: The browser's localStorage to start with, by origin, then
                by key.
            proxy: The proxy to bind, such as `http://user:pass@proxy:8080`.
            profile: The Profile to bind, such as `chrome150`.
            account: The Account to log in with.
        """
        self.id = id or uuid.uuid4().hex
        self.cookies = CookieJar(cookies)
        self.local_storage = {origin: dict(items) for origin, items in (local_storage or {}).items()}
        self.proxy = proxy
        self.profile = profile
        self.account = account

    @classmethod
    def from_state(cls, id: str, state: SessionState) -> Self:
        """Create a Session from its stored state.

        Args:
            id: The identifier.
            state: The state.

        Returns:
            The Session.
        """
        return cls(
            id,
            cookies=state.cookies,
            local_storage=state.local_storage,
            proxy=state.proxy,
            profile=state.profile,
            account=state.account,
        )

    def state(self) -> SessionState:
        """Return the Session's state, to store it or move it to another process.

        Returns:
            A copy of the state.
        """
        return SessionState(
            cookies=list(self.cookies),
            local_storage={origin: dict(items) for origin, items in self.local_storage.items()},
            proxy=self.proxy,
            profile=self.profile,
            account=self.account,
        )

    def __repr__(self) -> str:
        described = _described(len(self.cookies), self.local_storage, self.proxy, self.profile, self.account)
        return f"Session({self.id!r}, {described})"


def _described(
    cookies: int, local_storage: Mapping[str, object], proxy: str | None, profile: str | None, account: str | None
) -> str:
    """What a `repr` shows of a Session: counts, and the proxy without its credentials."""
    parts = [f"cookies={cookies}", f"local_storage={len(local_storage)} origins"]
    if proxy is not None:
        parts.append(f"proxy={_without_credentials(proxy)!r}")
    if profile is not None:
        parts.append(f"profile={profile!r}")
    if account is not None:
        parts.append(f"account={account!r}")
    return ", ".join(parts)


def _without_credentials(proxy: str) -> str:
    """A proxy address with whatever precedes its host masked, with or without a scheme."""
    authority, at, host = proxy.rpartition("@")
    if not at:
        return proxy
    scheme, separator, _ = authority.rpartition("://")
    return f"{scheme}{separator}{MASK}@{host}"
