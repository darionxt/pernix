"""Cookies held by name, as `Request.cookies` and `Response.cookies` carry them.

Parsing follows RFC 6265 §5.2 for `Set-Cookie` and §5.4 for `Cookie`, reduced to what
a name → value snapshot needs: domain and path are not tracked, and of the cookie
attributes only `Max-Age` and `Expires` are interpreted. Cookie values never appear
in log messages.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["SetCookie", "merge_set_cookies", "parse_cookie_header", "parse_set_cookie"]

logger = logging.getLogger(__name__)

_MAX_AGE = re.compile(r"-?\d+")
_MAX_AGE_DIGITS = 18
"""Longer `Max-Age` values are not converted: they only decide between expired and kept."""


@dataclass(frozen=True, slots=True)
class SetCookie:
    """One cookie from a `Set-Cookie` header.

    Attributes:
        name: The cookie name.
        value: The cookie value, exactly as sent, including any quotes.
        max_age: Seconds until the cookie expires, if `Max-Age` was given.
        expires: When the cookie expires, if a parsable `Expires` was given.
    """

    name: str
    value: str
    max_age: int | None = None
    expires: dt.datetime | None = None

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
    max_age: int | None = None
    expires: dt.datetime | None = None
    for attribute in attributes.split(";"):
        key, _, attribute_value = attribute.partition("=")
        key, attribute_value = key.strip().lower(), attribute_value.strip()
        if key == "max-age" and _MAX_AGE.fullmatch(attribute_value):
            max_age = _bounded_seconds(attribute_value)
        elif key == "expires" and (parsed := _parse_date(attribute_value)) is not None:
            expires = parsed
    return SetCookie(name, value, max_age, expires)


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
    if len(text.lstrip("-")) <= _MAX_AGE_DIGITS:
        return int(text)
    return -1 if text.startswith("-") else 10**_MAX_AGE_DIGITS


def _parse_date(value: str) -> dt.datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
