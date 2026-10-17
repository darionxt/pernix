"""A cookie jar that keeps full cookie records, set and matched to URLs as browsers do.

Storing follows RFC 6265bis §5.7 and matching §5.8.3, as Chrome applies them:
`Domain` attributes naming a public suffix or another host are refused, Secure
cookies come only from secure origins and never give way to insecure ones, the
`__Secure-` and `__Host-` prefixes are enforced, and expiry is capped at 400 days.
`SameSite` is kept but not applied: it matters to the browser, while an HTTP fetch
is a navigation of its own. Unlike browsers, the jar does not limit how many cookies
a domain holds. Cookie values never appear in a `repr`.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
import time
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Self
from urllib.parse import unquote, urlsplit

import msgspec
from ada_url import idna_to_ascii
from publicsuffixlist import PublicSuffixList

from pernix.cookies import SameSite, SetCookie, parse_set_cookie, requires_secure
from pernix.urls import is_loopback_host

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

__all__ = ["Cookie", "CookieJar", "CookieKey", "cookie_host", "cookie_key"]

_MAX_SIZE = 4096
"""Bytes a cookie's name and value may have together, as browsers take at most."""

_MAX_LIFETIME = 400 * 24 * 3600
"""Seconds a cookie may last at most, as Chrome caps it."""

_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
"""The control characters a cookie may not hold: all but the tab (RFC 6265bis §5.6)."""

type CookieKey = tuple[str, bool, str, str]
"""What tells a cookie from the others in a jar: its domain, host-only flag, path, and name."""

_COOKIE_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_SECURE_SCHEMES = frozenset({"https", "wss"})


class Cookie(msgspec.Struct, frozen=True, kw_only=True):
    """One cookie, with every attribute a browser keeps.

    Attributes:
        name: The cookie name.
        value: The cookie value.
        domain: The host the cookie belongs to, or the domain whose hosts it goes
            to: lower-cased, in ASCII, without a leading dot.
        host_only: Whether it goes to `domain` only, rather than to the hosts under
            it too.
        path: The path it goes to, with the paths under it.
        expires: When it expires, in seconds since the epoch; `None` for one that
            lasts as long as its jar.
        secure: Whether it goes over secure connections only: HTTPS, and to
            loopback hosts.
        http_only: Whether scripts in the browser are kept from it.
        same_site: Its `SameSite` attribute; `None` when unspecified.
        created: When it was first set, in seconds since the epoch; a cookie that
            replaces another keeps the time of the one it replaced.
        modified: When it was last set, in seconds since the epoch.
    """

    name: str
    value: str
    domain: str
    host_only: bool = True
    path: str = "/"
    expires: float | None = None
    secure: bool = False
    http_only: bool = False
    same_site: SameSite | None = None
    created: float = msgspec.field(default_factory=time.time)
    modified: float = msgspec.field(default_factory=time.time)

    def __repr__(self) -> str:
        return f"Cookie(name={self.name!r}, value='***', domain={self.domain!r}, path={self.path!r})"


class CookieJar:
    """Cookies kept by name, domain, host-only flag, and path, as browsers keep them.

    `receive()` applies the `Set-Cookie` headers of a response, and `matching()` gives
    the cookies a browser would send to a URL. Iterating gives the cookies in the
    order they were first set.
    """

    def __init__(self, cookies: Iterable[Cookie] = ()) -> None:
        """Create a jar.

        Args:
            cookies: Cookies to keep, as `set()` keeps them.
        """
        self._cookies: dict[CookieKey, Cookie] = {}
        for cookie in cookies:
            self.set(cookie)

    def __iter__(self) -> Iterator[Cookie]:
        return iter(list(self._cookies.values()))

    def __len__(self) -> int:
        return len(self._cookies)

    def __repr__(self) -> str:
        return f"CookieJar({len(self)} cookies)"

    def set(self, cookie: Cookie) -> None:
        """Keep a cookie as it is, replacing the one with its name, domain, host-only flag, and path.

        Args:
            cookie: The cookie.
        """
        self._cookies[cookie_key(cookie)] = cookie

    def receive(self, url: str, set_cookie_headers: Iterable[str], *, now: float | None = None) -> None:
        """Apply the `Set-Cookie` headers of a response to the jar, in order, as a browser would.

        A cookie is refused when it is larger than 4096 bytes or holds a control
        character other than the tab, when its `Domain` is
        a public suffix or another host than the URL's, when it is Secure but the
        URL is not secure, when it is `SameSite=None` without being Secure, when its
        name has a prefix whose rules it breaks, or when it would replace a Secure
        cookie from an insecure URL. A cookie that has expired removes the one it
        would replace.

        Args:
            url: The URL of the request the response answered.
            set_cookie_headers: The `Set-Cookie` header values.
            now: The current time, in seconds since the epoch; the system clock's by
                default.
        """
        origin = _Origin.of(url)
        if origin is None:
            return
        moment = time.time() if now is None else now
        for header in set_cookie_headers:
            parsed = parse_set_cookie(header)
            if parsed is not None:
                self._receive(parsed, origin, moment)

    def matching(self, url: str, *, now: float | None = None) -> list[Cookie]:
        """Return the cookies a browser would send to a URL.

        Args:
            url: The URL.
            now: The current time, in seconds since the epoch; the system clock's by
                default.

        Returns:
            The cookies, in the order a browser sends them: longer paths first, then
            the earliest set first.
        """
        origin = _Origin.of(url)
        if origin is None:
            return []
        moment = time.time() if now is None else now
        found = [cookie for cookie in self._cookies.values() if origin.takes(cookie, moment)]
        return sorted(found, key=lambda cookie: (-len(cookie.path), cookie.created))

    def clear_expired(self, *, now: float | None = None) -> None:
        """Remove the cookies that have expired.

        Args:
            now: The current time, in seconds since the epoch; the system clock's by
                default.
        """
        moment = time.time() if now is None else now
        for key, cookie in list(self._cookies.items()):
            if _expired(cookie, moment):
                del self._cookies[key]

    def _receive(self, parsed: SetCookie, origin: _Origin, now: float) -> None:
        if len(parsed.name.encode()) + len(parsed.value.encode()) > _MAX_SIZE or _CONTROL.search(
            parsed.name + parsed.value
        ):
            return
        scope = _scope(parsed.domain, origin.host)
        if scope is None:
            return
        domain, host_only = scope
        path = parsed.path or _default_path(origin.path)
        if not _allowed(parsed, origin, host_only=host_only):
            return
        if not origin.secure and not parsed.secure and self._shadows_secure(parsed.name, domain, path, now):
            return
        key = (domain, host_only, path, parsed.name)
        if parsed.expired(dt.datetime.fromtimestamp(now, dt.UTC)):
            self._cookies.pop(key, None)
            return
        replaced = self._cookies.get(key)
        self._cookies[key] = Cookie(
            name=parsed.name,
            value=parsed.value,
            domain=domain,
            host_only=host_only,
            path=path,
            expires=_expiry(parsed, now),
            secure=parsed.secure,
            http_only=parsed.http_only,
            same_site=parsed.same_site,
            created=now if replaced is None else replaced.created,
            modified=now,
        )

    def _shadows_secure(self, name: str, domain: str, path: str, now: float) -> bool:
        """Whether an insecure cookie would overwrite a live Secure one of that name: RFC 6265bis §5.7, step 16."""
        return any(
            cookie.name == name
            and cookie.secure
            and not _expired(cookie, now)
            and (_domain_matches(cookie.domain, domain) or _domain_matches(domain, cookie.domain))
            and _path_matches(path, cookie.path)
            for cookie in self._cookies.values()
        )


def cookie_host(url: str) -> str | None:
    """Return the host of a URL as cookies are matched to it: lower-cased, IPv6 without brackets, and in ASCII.

    The host is the one curl connects to: after the last `@` of the authority, even
    where WHATWG URLs would end the authority earlier, such as at a backslash, and
    percent-decoded. One that is not ASCII is then converted as WHATWG URLs convert
    hosts, without the transitional IDNA mappings, as curl does too: `faß` is
    `xn--fa-hia`, not `fass`.

    Args:
        url: The URL.

    Returns:
        The host, or `None` for none, or for one that WHATWG URLs reject, which curl
        converts differently on each platform.
    """
    parts = urlsplit(url)
    authority = parts.netloc.rpartition("@")[2]
    if authority.startswith("["):
        return parts.hostname  # an IPv6 address
    host = unquote(authority.partition(":")[0])  # not lower-cased yet, which would turn a final Σ into ς
    if host.isascii():
        return host.lower() or None
    return _ascii(host)


@dataclass(frozen=True, slots=True)
class _Origin:
    """What matching a cookie needs of a URL."""

    host: str
    path: str
    secure: bool

    @classmethod
    def of(cls, url: str) -> Self | None:
        scheme = urlsplit(url).scheme.lower()
        host = cookie_host(url)
        if scheme not in _COOKIE_SCHEMES or host is None:
            return None
        return cls(host, urlsplit(url).path or "/", scheme in _SECURE_SCHEMES or is_loopback_host(host))

    def takes(self, cookie: Cookie, now: float) -> bool:
        """Whether a browser sends the cookie to this URL."""
        if _expired(cookie, now) or (cookie.secure and not self.secure):
            return False
        on_host = self.host == cookie.domain if cookie.host_only else _domain_matches(self.host, cookie.domain)
        return on_host and _path_matches(self.path, cookie.path)


def cookie_key(cookie: Cookie) -> CookieKey:
    """Return what tells a cookie from the others in a jar.

    Args:
        cookie: The cookie.

    Returns:
        Its domain, host-only flag, path, and name.
    """
    return (cookie.domain, cookie.host_only, cookie.path, cookie.name)


def _scope(attribute: str | None, host: str) -> tuple[str, bool] | None:
    """The domain a cookie belongs to, and whether it is host-only; `None` if its `Domain` is refused."""
    if attribute is None:
        return host, True
    domain = attribute if attribute.isascii() else _ascii(attribute)
    if domain is None:
        return None
    if _is_ip(host) or _public_suffixes().is_public(domain):
        return (host, True) if domain == host else None  # only the host itself, as a host-only cookie
    return (domain, False) if _domain_matches(host, domain) else None


def _allowed(parsed: SetCookie, origin: _Origin, *, host_only: bool) -> bool:
    """Whether a cookie meets the rules of Secure, `SameSite=None`, and the name prefixes.

    A `__Host-` cookie needs an explicit `Path=/`: the default path does not do.
    """
    if parsed.secure and not origin.secure:
        return False
    if parsed.same_site == "none" and not parsed.secure:
        return False
    if requires_secure(parsed.name) and not parsed.secure:
        return False
    return not parsed.name.lower().startswith("__host-") or (host_only and parsed.path == "/")


def _expiry(parsed: SetCookie, now: float) -> float | None:
    """When a cookie that has not expired expires, capped at 400 days; `None` for one without an expiry."""
    if parsed.max_age is not None:
        return now + min(parsed.max_age, _MAX_LIFETIME)
    if parsed.expires is not None:
        return min(parsed.expires.timestamp(), now + _MAX_LIFETIME)
    return None


def _expired(cookie: Cookie, now: float) -> bool:
    return cookie.expires is not None and cookie.expires <= now


def _default_path(path: str) -> str:
    """The path a cookie without a valid `Path` goes to: the directory of the request path (RFC 6265 §5.1.4)."""
    if not path.startswith("/") or path.count("/") == 1:
        return "/"
    return path[: path.rindex("/")]


def _domain_matches(host: str, domain: str) -> bool:
    """Whether a host is the domain, or a name under it (RFC 6265 §5.1.3)."""
    return host == domain or (host.endswith(f".{domain}") and not _is_ip(host))


def _path_matches(request_path: str, cookie_path: str) -> bool:
    """Whether a request path is the cookie's path, or a path under it (RFC 6265 §5.1.4)."""
    if request_path == cookie_path:
        return True
    return request_path.startswith(cookie_path) and (cookie_path.endswith("/") or request_path[len(cookie_path)] == "/")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _ascii(name: str) -> str | None:
    """A domain name in ASCII, as WHATWG URLs convert it; `None` for one they reject."""
    return idna_to_ascii(name).decode("ascii") or None


@cache
def _public_suffixes() -> PublicSuffixList:
    """The Public Suffix List, loaded on first use."""
    return PublicSuffixList()
