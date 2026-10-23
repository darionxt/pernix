"""Following redirects as browsers do: which responses redirect, where to, and how the request changes.

The rules are those of the Fetch standard's HTTP-redirect fetch, as Chrome applies them
to navigations: 301, 302, 303, 307, and 308 redirect when they carry a `Location`, which
is resolved against the URL that answered as WHATWG URLs resolve it. 301 and 302 turn a
POST into a GET, and 303 turns anything but GET and HEAD into one, without the body and
the headers that describe it; 307 and 308 keep the method and the body. Credentials in
headers never follow a redirect to another origin.

A fetch follows as many redirects as `http.max_redirects` allows, or fewer inside
`following_at_most()`, as a robots.txt fetch does. Inside `checking_redirects()`, each
redirect is checked before it is followed, as the robots.txt layer checks it against the
robots.txt of the origin it leads to; a Fetcher that follows redirects itself reads the
check with `redirect_check()`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ada_url import URL
from curl_cffi import CurlECode

from pernix.errors import FetchError
from pernix.headers import Headers
from pernix.urls import url_host

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Generator

__all__ = [
    "REDIRECT_STATUSES",
    "Hop",
    "RedirectCheck",
    "checking_redirects",
    "following_at_most",
    "redirect_check",
    "redirect_limit",
    "redirect_target",
    "redirected",
]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
"""The statuses browsers follow: other 3xx responses, such as 300 or 304, are final."""

_FOLLOWED_SCHEMES = frozenset({"http", "https"})

_BODY_HEADERS = frozenset({"content-encoding", "content-language", "content-location", "content-type"})
"""The headers that describe a request body, dropped with it: the Fetch standard's request-body-header names."""

_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization"})
"""The headers dropped on a redirect to another origin."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

type RedirectCheck = Callable[[str], Awaitable[None]]
"""Checks the URL a redirect leads to before the redirect is followed, raising to refuse it."""

_limit: ContextVar[int | None] = ContextVar("pernix_redirect_limit", default=None)
"""The redirects the fetches the current task makes may follow at most, below the Fetcher's own limit, if any."""

_check: ContextVar[RedirectCheck | None] = ContextVar("pernix_redirect_check", default=None)
"""What checks the redirects of the fetches the current task makes, if anything."""


@contextmanager
def following_at_most(redirects: int) -> Generator[None]:
    """Make the fetches inside the block follow at most so many redirects, if the Fetcher's own limit is higher.

    The HTTP Fetcher reads it: RFC 9309 has a robots.txt fetch follow five.

    Args:
        redirects: The redirects to follow at most.

    Yields:
        Nothing; the block runs with the limit in effect.
    """
    token = _limit.set(redirects)
    try:
        yield
    finally:
        _limit.reset(token)


def redirect_limit(configured: int) -> int | None:
    """Return the limit `following_at_most()` sets for the current fetch, if it is below the Fetcher's own.

    Args:
        configured: The Fetcher's own limit, such as `http.max_redirects`.

    Returns:
        The lower limit; `None` when the Fetcher's own applies.
    """
    limit = _limit.get()
    return limit if limit is not None and limit < configured else None


@contextmanager
def checking_redirects(check: RedirectCheck | None) -> Generator[None]:
    """Make the fetches inside the block check each redirect before they follow it, or check none.

    The robots.txt layer checks the redirects of each fetch it checks so
    (`pernix.robots`), and those of the other fetches none, so that a fetch never
    takes the check of the one it is nested in.

    Args:
        check: Called with the URL each redirect leads to, before the hop to it
            is sent; what it raises fails the fetch. `None` checks none.

    Yields:
        Nothing; the block runs with the check in effect.
    """
    token = _check.set(check)
    try:
        yield
    finally:
        _check.reset(token)


def redirect_check() -> RedirectCheck | None:
    """Return what checks the redirects of the current fetch, as `checking_redirects()` set it, if anything.

    The HTTP Fetcher and the browser Fetcher call it with the URL of each redirect
    before they follow it; a Fetcher that follows redirects itself does as they do.

    Returns:
        The check; `None` when none is in effect.
    """
    return _check.get()


@dataclass(frozen=True, slots=True)
class Hop:
    """One request of a redirect chain.

    Attributes:
        method: The HTTP method.
        url: The URL.
        headers: The request headers, in wire order.
        body: The request body, if any.
    """

    method: str
    url: str
    headers: Headers
    body: bytes | None


def redirect_target(url: str, status: int, location: str | None) -> str | None:
    """Return the URL a response redirects to, as browsers resolve it.

    Args:
        url: The URL that answered.
        status: The response status.
        location: The response's first non-empty `Location` value, if any.

    Returns:
        The URL, absolute and serialized as WHATWG URLs serialize it, or `None` for a
        response that does not redirect.

    Raises:
        pernix.errors.FetchError: The `Location` is not a valid URL (code 3), or names
            neither an http nor an https URL (code 1), which browsers refuse to follow.
    """
    if status not in REDIRECT_STATUSES or not location:
        return None
    try:
        target = URL(location, url)
    except ValueError:
        raise FetchError(
            f"the redirect from {url} leads to {location!r}, which is not a valid URL", code=CurlECode.URL_MALFORMAT
        ) from None
    if target.protocol.removesuffix(":") not in _FOLLOWED_SCHEMES:
        raise FetchError(
            f"the redirect from {url} leads to {target.href}, which is neither an http nor an https URL",
            code=CurlECode.UNSUPPORTED_PROTOCOL,
        )
    return target.href


def redirected(hop: Hop, status: int, target: str) -> Hop:
    """Return the request a redirect leads to.

    Args:
        hop: The request that was redirected.
        status: The redirect status.
        target: The URL it redirects to, as `redirect_target()` gives it.

    Returns:
        The request to send: a GET without a body, or the headers describing one,
        when the status turns the method into GET, and without `Authorization` or
        `Proxy-Authorization` when the target is on another origin.
    """
    to_get = (status in (301, 302) and hop.method == "POST") or (status == 303 and hop.method not in ("GET", "HEAD"))
    dropped = (_BODY_HEADERS if to_get else frozenset[str]()) | (
        frozenset[str]() if _origin(hop.url) == _origin(target) else _CREDENTIAL_HEADERS
    )
    headers = Headers((name, value) for name, value in hop.headers if name.lower() not in dropped)
    return Hop("GET" if to_get else hop.method, target, headers, None if to_get else hop.body)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    """The scheme, the host curl connects to, and the port of a URL; -1 for a port that is not valid."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        return scheme, url_host(url), -1  # the origin of no other URL
    return scheme, url_host(url), port or _DEFAULT_PORTS.get(scheme)
