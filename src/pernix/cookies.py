from __future__ import annotations

import datetime as dt
import logging
from email.utils import parsedate_to_datetime
from http.cookies import CookieError, Morsel, SimpleCookie
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = ["merge_set_cookies"]

logger = logging.getLogger(__name__)


def merge_set_cookies(
    cookies: Mapping[str, str],
    set_cookie_headers: Iterable[str],
    *,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    """Apply `Set-Cookie` header values, in order, to cookies held by name.

    A cookie is set or replaced by name; one with `Max-Age` of zero or less, or an
    `Expires` date that has passed, is removed. Domain and path are not tracked, so
    same-named cookies for different domains or paths collapse into one. Values that
    cannot be parsed are skipped.

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
        parsed: SimpleCookie = SimpleCookie()
        try:
            parsed.load(header)
        except CookieError:
            logger.debug("Ignoring unparsable Set-Cookie header %r", header)
            continue
        for name, morsel in parsed.items():
            if _expired(morsel, moment):
                current.pop(name, None)
            else:
                current[name] = morsel.value
    return current


def _expired(morsel: Morsel[str], now: dt.datetime) -> bool:
    max_age = morsel["max-age"]
    if max_age:
        try:
            return int(max_age) <= 0
        except ValueError:
            pass
    expires = morsel["expires"]
    if expires:
        try:
            return parsedate_to_datetime(expires) <= now
        except (TypeError, ValueError):
            return False
    return False
