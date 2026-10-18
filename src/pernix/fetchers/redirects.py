"""Following redirects as browsers do: which responses redirect, where to, and how the request changes.

The rules are those of the Fetch standard's HTTP-redirect fetch, as Chrome applies them
to navigations: 301, 302, 303, 307, and 308 redirect when they carry a `Location`, which
is resolved against the URL that answered as WHATWG URLs resolve it. 301 and 302 turn a
POST into a GET, and 303 turns anything but GET and HEAD into one, without the body and
the headers that describe it; 307 and 308 keep the method and the body. Credentials in
headers never follow a redirect to another origin.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from ada_url import URL
from curl_cffi import CurlECode

from pernix.errors import FetchError
from pernix.headers import Headers
from pernix.urls import url_host

__all__ = ["REDIRECT_STATUSES", "Hop", "redirect_target", "redirected"]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
"""The statuses browsers follow: other 3xx responses, such as 300 or 304, are final."""

_FOLLOWED_SCHEMES = frozenset({"http", "https"})

_BODY_HEADERS = frozenset({"content-encoding", "content-language", "content-location", "content-type"})
"""The headers that describe a request body, dropped with it: the Fetch standard's request-body-header names."""

_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization"})
"""The headers dropped on a redirect to another origin."""

_DEFAULT_PORTS = {"http": 80, "https": 443}


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
