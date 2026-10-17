"""Cookies held by name, as `Request.cookies` and `Response.cookies` carry them.

Parsing follows RFC 6265 §5.2 for `Set-Cookie` and §5.4 for `Cookie`. A parsed
`Set-Cookie` holds every attribute a cookie jar (`pernix.cookie_jar`) needs, while
the name → value snapshot of `merge_set_cookies()` tracks neither domain nor path,
and interprets only `Max-Age` and `Expires`. Cookie values never appear in log
messages.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from pernix.request import Request

__all__ = [
    "SameSite",
    "SetCookie",
    "check_cookies",
    "merge_set_cookies",
    "parse_cookie_header",
    "parse_set_cookie",
    "request_cookies",
    "requires_secure",
]

logger = logging.getLogger(__name__)

_MAX_AGE = re.compile(r"-?\d+")
_MAX_AGE_DIGITS = 18
"""Longer `Max-Age` values are not converted: they only decide between expired and kept."""

_NOT_IN_NAME = re.compile(r"[\x00-\x1f\x7f;=]")
_NOT_IN_VALUE = re.compile(r"[\x00-\x1f\x7f;]")

_SECURE_PREFIXES = ("__secure-", "__host-")

type SameSite = Literal["strict", "lax", "none"]
"""The `SameSite` attribute of a cookie, lower-cased."""

_SAME_SITE: dict[str, SameSite] = {"strict": "strict", "lax": "lax", "none": "none"}


@dataclass(frozen=True, slots=True)
class SetCookie:
    """One cookie from a `Set-Cookie` header.

    Attributes:
        name: The cookie name.
        value: The cookie value, exactly as sent, including any quotes.
        max_age: Seconds until the cookie expires, if `Max-Age` was given.
        expires: When the cookie expires, if a parsable `Expires` was given.
        domain: The `Domain` attribute, without its leading dot and lower-cased;
            `None` without one.
        path: The `Path` attribute; `None` without one, or for one that does not
            start with `/`, which means the default path.
        secure: Whether the cookie is `Secure`.
        http_only: Whether the cookie is `HttpOnly`.
        same_site: The `SameSite` attribute; `None` without one, or for an unknown
            value.
    """

    name: str
    value: str
    max_age: int | None = None
    expires: dt.datetime | None = None
    domain: str | None = None
    path: str | None = None
    secure: bool = False
    http_only: bool = False
    same_site: SameSite | None = None

    def expired(self, now: dt.datetime) -> bool:
        """Whether the server asked to remove the cookie. `Max-Age` takes precedence over `Expires`.

        Args:
            now: The current time, timezone-aware.

        Returns:
            `True` for a non-positive `Max-Age` or an `Expires` in the past.
        """
        if self.max_age is not None:
            return self.max_age <= 0
        return self.expires is not None and self.expires <= now


def parse_set_cookie(header: str) -> SetCookie | None:
    """Parse one `Set-Cookie` header value into one cookie.

    Everything before the first `;` is the `name=value` pair; the rest are attributes.
    Unknown attributes, such as `Partitioned` or `Priority`, are ignored. When an
    attribute repeats, its last valid occurrence wins.

    Args:
        header: The header value.

    Returns:
        The cookie, or `None` if the header has no `name=value` pair or the name is
        empty.
    """
    pair, _, attributes = header.partition(";")
    name, separator, value = pair.partition("=")
    name, value = name.strip(), value.strip()
    if not separator or not name:
        return None
    fields: dict[str, Any] = {}
    for attribute in attributes.split(";"):
        key, _, attribute_value = attribute.partition("=")
        key, attribute_value = key.strip().lower(), attribute_value.strip()
        if key == "max-age" and _MAX_AGE.fullmatch(attribute_value):
            fields["max_age"] = _bounded_seconds(attribute_value)
        elif key == "expires" and (parsed := _parse_date(attribute_value)) is not None:
            fields["expires"] = parsed
        elif key == "domain" and attribute_value:  # an empty one is ignored
            fields["domain"] = attribute_value.removeprefix(".").lower()
        elif key == "path":
            fields["path"] = attribute_value if attribute_value.startswith("/") else None
        elif key in ("secure", "httponly"):
            fields["secure" if key == "secure" else "http_only"] = True
        elif key == "samesite":
            fields["same_site"] = _SAME_SITE.get(attribute_value.lower())
    return SetCookie(name, value, **fields)


def parse_cookie_header(header: str) -> dict[str, str]:
    """Parse a `Cookie` request header value into cookies by name.

    Args:
        header: The header value, such as `"session=abc; lang=en"`.

    Returns:
        The cookies by name; pairs without `=` or with an empty name are skipped.
    """
    cookies: dict[str, str] = {}
    for pair in header.split(";"):
        name, separator, value = pair.partition("=")
        name = name.strip()
        if separator and name:
            cookies[name] = value.strip()
    return cookies


def request_cookies(request: Request) -> dict[str, str]:
    """Return the cookies a Request carries, whether as `cookies=` or as `Cookie` headers.

    Args:
        request: The Request.

    Returns:
        The cookies by name, in the order the Request gives them.
    """
    sent: dict[str, str] = {}
    for header in request.headers.get_all("Cookie"):
        sent.update(parse_cookie_header(header))
    sent.update(request.cookies)
    return sent


def check_cookies(cookies: Mapping[str, str]) -> None:
    """Refuse cookies that a `Cookie` header cannot carry as given.

    Args:
        cookies: The cookies by name.

    Raises:
        ValueError: A name is empty or has `=`, `;`, or a control character, or a
            value has `;` or a control character. The message names the cookie, never
            its value.
    """
    for name, value in cookies.items():
        if not name or _NOT_IN_NAME.search(name):
            raise ValueError(f"the cookie name {name!r} is empty or has '=', ';', or a control character")
        if _NOT_IN_VALUE.search(value):
            raise ValueError(f"the value of the cookie {name!r} has ';' or a control character")


def requires_secure(name: str) -> bool:
    """Whether browsers take a cookie of this name only when it is Secure.

    Args:
        name: The cookie name.

    Returns:
        `True` for names starting with `__Secure-` or `__Host-`, in any case.
    """
    return name.lower().startswith(_SECURE_PREFIXES)


def merge_set_cookies(
    cookies: Mapping[str, str],
    set_cookie_headers: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """Apply `Set-Cookie` header values, in order, to cookies held by name.

    A cookie is set or replaced by name, and removed when it has expired. Domain and
    path are not tracked, so same-named cookies for different domains or paths
    collapse into one. Headers without a `name=value` pair are skipped.

    Args:
        cookies: The cookies held before, by name.
        set_cookie_headers: `Set-Cookie` header values, oldest first.
        now: The current time, for `Expires`; defaults to the system clock.

    Returns:
        The cookies held afterwards, by name.
    """
    current = dict(cookies)
    moment = now or dt.datetime.now(dt.UTC)
    for header in set_cookie_headers:
        cookie = parse_set_cookie(header)
        if cookie is None:
            logger.debug("Ignoring a Set-Cookie header without a name=value pair")
        elif cookie.expired(moment):
            current.pop(cookie.name, None)
        else:
            current[cookie.name] = cookie.value
    return current


def _bounded_seconds(text: str) -> int:
    """Convert a `Max-Age` value, clamping very long ones instead of building huge integers."""
    negative = text.startswith("-")
    significant = text.lstrip("-").lstrip("0")
    if not significant:
        return 0
    if len(significant) > _MAX_AGE_DIGITS:
        return -1 if negative else 10**_MAX_AGE_DIGITS
    value = int(significant)  # never the zero-padded text, which may exceed the conversion limit
    return -value if negative else value


def _parse_date(value: str) -> dt.datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
