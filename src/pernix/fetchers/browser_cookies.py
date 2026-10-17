"""Cookies between a cookie jar and a browser context: as the browser takes them, and what a page changed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from pernix.cookie_jar import Cookie, cookie_key
from pernix.cookies import requires_secure

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from patchright._impl._api_structures import Cookie as BrowserCookie
    from patchright._impl._api_structures import SetCookieParam

    from pernix.cookie_jar import CookieJar, CookieKey
    from pernix.cookies import SameSite

__all__ = ["BrowserKey", "apply_page_changes", "browser_cookie", "browser_key", "changes", "keyed", "request_cookie"]

type BrowserKey = tuple[str, str, str]
"""What tells a cookie from the others in a browser: its name, its domain as the browser writes it, and its path."""

type _Fields = tuple[str, float | None, bool, bool, SameSite | None]
"""The attributes of a cookie a page may change: value, expiry, HttpOnly, Secure, and SameSite."""

_SAME_SITE: dict[SameSite, Literal["Strict", "Lax", "None"]] = {"strict": "Strict", "lax": "Lax", "none": "None"}

_UNSPECIFIED: Literal["Lax"] = "Lax"
"""The SameSite the browser reports for a cookie that has none."""

_SESSION_EXPIRY = -1.0
"""The expiry the browser reports for a cookie that lasts as long as the browser."""

_EXPIRY_TOLERANCE = 1.0
"""Seconds an expiry may differ by and still be the same, as the browser rounds it."""


def browser_cookie(cookie: Cookie) -> SetCookieParam:
    """Write a jar's cookie as the browser takes it.

    A domain cookie's domain gets a leading dot, and an IPv6 host its brackets. An
    unspecified SameSite is left out, so the browser holds it unspecified too.

    Args:
        cookie: The cookie.

    Returns:
        The cookie, for `BrowserContext.add_cookies()`.
    """
    param: SetCookieParam = {
        "name": cookie.name,
        "value": cookie.value,
        "domain": _browser_domain(cookie.domain, host_only=cookie.host_only),
        "path": cookie.path,
        "httpOnly": cookie.http_only,
        "secure": cookie.secure,
    }
    if cookie.expires is not None:
        param["expires"] = cookie.expires
    if cookie.same_site is not None:
        param["sameSite"] = _SAME_SITE[cookie.same_site]
    return param


def request_cookie(host: str, name: str, value: str) -> SetCookieParam:
    """Write one of a Request's own cookies as the browser takes it: held for its host only, for every path.

    It is not Secure, so it goes over HTTP as well as HTTPS, unless its name has a
    prefix the browser takes only on Secure cookies.

    Args:
        host: The host of the Request, as `pernix.urls.url_host()` gives it.
        name: The cookie name.
        value: The cookie value.

    Returns:
        The cookie, for `BrowserContext.add_cookies()`.
    """
    return {
        "name": name,
        "value": value,
        "domain": _browser_domain(host, host_only=True),
        "path": "/",
        "secure": requires_secure(name),
    }


def keyed(cookies: Iterable[BrowserCookie]) -> dict[BrowserKey, BrowserCookie]:
    """Key the cookies a browser context holds, leaving out the partitioned ones, which a jar does not keep.

    Args:
        cookies: The cookies, from `BrowserContext.cookies()`.

    Returns:
        The cookies, by key.
    """
    return {_key(cookie): cookie for cookie in cookies if not cookie.get("partitionKey")}


def changes(
    held: Mapping[BrowserKey, BrowserCookie], wanted: Mapping[BrowserKey, SetCookieParam]
) -> list[SetCookieParam]:
    """Tell what to add to a browser context so that it holds the wanted cookies, and no other.

    Args:
        held: The cookies the context holds, from `keyed()`.
        wanted: The cookies it is to hold, by key.

    Returns:
        The wanted cookies it holds otherwise or not at all, then those it holds
        and is not to, expired, which removes them; empty if it holds them already.
    """
    added = [cookie for key, cookie in wanted.items() if key not in held or not _same(held[key], cookie)]
    removed: list[SetCookieParam] = [
        {
            "name": cookie.get("name", ""),
            "value": "",
            "domain": cookie.get("domain", ""),
            "path": cookie.get("path", "/"),
            "secure": cookie.get("secure", False),
            "httpOnly": cookie.get("httpOnly", False),
            "expires": 0,
        }
        for key, cookie in held.items()
        if key not in wanted
    ]
    return added + removed


def browser_key(cookie: SetCookieParam) -> BrowserKey:
    """Key a cookie written for the browser.

    Args:
        cookie: The cookie, from `browser_cookie()` or `request_cookie()`.

    Returns:
        Its key.
    """
    return cookie.get("name", ""), cookie.get("domain") or "", cookie.get("path") or "/"


def apply_page_changes(
    jar: CookieJar,
    before: Mapping[BrowserKey, BrowserCookie],
    after: Mapping[BrowserKey, BrowserCookie],
    *,
    now: float,
) -> None:
    """Make in a jar the changes a page made to the cookies of its browser context.

    A cookie the page set or changed is kept with the attributes the browser says
    changed, and the jar's own for the others, which the browser may not tell, such
    as an unspecified SameSite, which it reports as Lax; it keeps the time the jar
    first held it. One the page removed, or that expired, is removed, the removal
    recorded. Cookies the page left as they were, a Request's own included, change
    nothing.

    Args:
        jar: The jar, such as a Session's.
        before: The cookies the context held when the page started, from `keyed()`.
        after: Those it holds now, from `keyed()`.
        now: The time of the changes, in seconds since the epoch.
    """
    held = {cookie_key(cookie): cookie for cookie in jar}
    for key, cookie in after.items():
        previous = before.get(key)
        if previous is not None and _fields(previous) == _fields(cookie):
            continue
        jar.set(_changed(cookie, previous, held.get(_jar_key(cookie)), now))
    for key, cookie in before.items():
        if key not in after:
            jar.discard(_jar_key(cookie), now=now)


def _changed(cookie: BrowserCookie, previous: BrowserCookie | None, held: Cookie | None, now: float) -> Cookie:
    """A cookie a page set or changed, as a jar keeps it."""
    fields = _fields(cookie)
    if previous is not None and held is not None:
        was, kept = _fields(previous), (held.value, held.expires, held.http_only, held.secure, held.same_site)
        fields = cast(
            "_Fields", tuple(new if new != old else own for new, old, own in zip(fields, was, kept, strict=True))
        )
    value, expires, http_only, secure, same_site = fields
    domain, host_only, path, name = _jar_key(cookie)
    return Cookie(
        name=name,
        value=value,
        domain=domain,
        host_only=host_only,
        path=path,
        expires=expires,
        secure=secure,
        http_only=http_only,
        same_site=same_site,
        created=now if held is None else held.created,
        modified=now,
    )


def _fields(cookie: BrowserCookie) -> _Fields:
    expires = cookie.get("expires", _SESSION_EXPIRY)
    same_site = cookie.get("sameSite", _UNSPECIFIED)
    return (
        cookie.get("value", ""),
        None if expires < 0 else expires,
        cookie.get("httpOnly", False),
        cookie.get("secure", False),
        cast("SameSite", same_site.lower()),
    )


def _same(held: BrowserCookie, wanted: SetCookieParam) -> bool:
    """Whether the browser holds a cookie as it is wanted, as it reports the wanted one once it holds it."""
    expires, wanted_expires = held.get("expires", _SESSION_EXPIRY), wanted.get("expires")
    same_expiry = expires < 0 if wanted_expires is None else abs(expires - wanted_expires) < _EXPIRY_TOLERANCE
    return (
        same_expiry
        and held.get("value") == wanted.get("value")
        and held.get("httpOnly", False) == bool(wanted.get("httpOnly"))
        and held.get("secure", False) == bool(wanted.get("secure"))
        and held.get("sameSite", _UNSPECIFIED) == (wanted.get("sameSite") or _UNSPECIFIED)
    )


def _key(cookie: BrowserCookie) -> BrowserKey:
    return cookie.get("name", ""), cookie.get("domain", ""), cookie.get("path", "/")


def _jar_key(cookie: BrowserCookie) -> CookieKey:
    """The key a jar holds a browser's cookie under: a domain written without its leading dot or brackets."""
    domain = cookie.get("domain", "")
    host = domain.removeprefix(".").removeprefix("[").removesuffix("]")
    return host, not domain.startswith("."), cookie.get("path", "/"), cookie.get("name", "")


def _browser_domain(domain: str, *, host_only: bool) -> str:
    """A jar's domain as the browser writes it: with a leading dot for a domain cookie, and an IPv6 host in brackets."""
    host = f"[{domain}]" if ":" in domain else domain
    return host if host_only else f".{host}"
