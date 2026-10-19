"""The headers Chrome sends for each kind of request, made for the HTTP Fetcher with the pinned Profile.

Each kind has the headers the bundled browser sends, in its order, with its
`Priority` and HTTP/2 stream weight, as measured on its requests (ADR 0010): a
top-level navigation, a form submission, an iframe's navigation, a `fetch()` or
`XMLHttpRequest`, and the loads of a style sheet, a script, and an image. The page
a request comes from, named by the Request's `Referer`, gives the `Referer` sent,
by Chrome's default policy, `strict-origin-when-cross-origin`, the `Origin`, and
`Sec-Fetch-Site`, over the whole redirect chain. A request without one is a
navigation typed in, or, for the other kinds, made by a page of its own origin that
sends no `Referer`.

To an origin that is not potentially trustworthy, such as plain `http://` to a host
other than a loopback one, Chrome sends no client hints and no `Sec-Fetch-*`, and
accepts only gzip and deflate. Over HTTP/1.1, curl puts `Host` and `Connection`
first, and sends no `Priority`; names keep Chrome's case there, and curl lowers
them over HTTP/2.

A header of the Request's own replaces the kind's of its name in place. Another
goes after the kind's headers in a navigation, and at the start of those the page
adds in the other kinds, in the Request's order: Chrome orders those by a hash of
their names, which this does not follow.
"""

from __future__ import annotations

import ipaddress
import itertools
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

from ada_url import URL
from publicsuffixlist import PublicSuffixList

from pernix.urls import is_loopback_host, url_host

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pernix.request_kinds import RequestKind

__all__ = ["ChromeRequest", "chrome_request", "site_relation"]

_ACCEPT_DOCUMENT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7"
)

_NAVIGATION = (
    "Host",
    "Connection",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "Upgrade-Insecure-Requests",
    "User-Agent",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-User",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
    "Priority",
)
"""The headers of a top-level navigation, in Chrome's order, `Host` and `Connection` going over HTTP/1.1 only."""

_FORM = (
    "Host",
    "Connection",
    "Content-Length",
    "Cache-Control",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "Upgrade-Insecure-Requests",
    "Content-Type",
    "User-Agent",
    "Origin",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-User",
    "Sec-Fetch-Dest",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
    "Priority",
)
"""The headers of a form submission."""

_IFRAME = (
    "Host",
    "Connection",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "Upgrade-Insecure-Requests",
    "User-Agent",
    "Accept",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Storage-Access",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
    "Priority",
)
"""The headers of an iframe's navigation."""

_PAGE_MADE = (
    "Host",
    "Connection",
    "Content-Length",
    "sec-ch-ua-platform",
    "User-Agent",
    "sec-ch-ua",
    "Content-Type",
    "sec-ch-ua-mobile",
    "Accept",
    "Origin",
    "Sec-Fetch-Site",
    "Sec-Fetch-Mode",
    "Sec-Fetch-Dest",
    "Sec-Fetch-Storage-Access",
    "Referer",
    "Accept-Encoding",
    "Accept-Language",
    "Cookie",
    "Priority",
)
"""The headers of a `fetch()` and of a load, those the page adds from `sec-ch-ua-platform` on."""

_PAGE_BLOCK = "sec-ch-ua-platform"
"""The first header a page adds, before which a Request's other headers go in the kinds a page makes."""


@dataclass(frozen=True, slots=True)
class _Shape:
    """What sets a kind of request apart."""

    order: tuple[str, ...]
    accept: str
    mode: str
    destination: str
    priority: str
    weight: int


_SHAPES: dict[RequestKind, _Shape] = {
    "navigation": _Shape(_NAVIGATION, _ACCEPT_DOCUMENT, "navigate", "document", "u=0, i", 256),
    "form": _Shape(_FORM, _ACCEPT_DOCUMENT, "navigate", "document", "u=0, i", 256),
    "iframe": _Shape(_IFRAME, _ACCEPT_DOCUMENT, "navigate", "iframe", "u=0, i", 256),
    "fetch": _Shape(_PAGE_MADE, "*/*", "cors", "empty", "u=1, i", 220),
    "style": _Shape(_PAGE_MADE, "text/css,*/*;q=0.1", "no-cors", "style", "u=0", 256),
    "script": _Shape(_PAGE_MADE, "*/*", "no-cors", "script", "u=1", 220),
    "image": _Shape(
        _PAGE_MADE,
        "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "no-cors",
        "image",
        "u=2, i",
        183,
    ),
}

_DERIVED = frozenset({"referer", "origin", "sec-fetch-dest", "host", "connection", "content-length", "cookie"})
"""Headers made from the request itself, which a Request's own does not replace.

Its `Referer` names the page it comes from, and its `Sec-Fetch-Dest` its kind.
"""

_SECURE_ONLY = (
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-fetch-site",
    "sec-fetch-mode",
    "sec-fetch-user",
    "sec-fetch-dest",
    "sec-fetch-storage-access",
)
"""Headers Chrome sends to potentially trustworthy origins only: the client hints and the fetch metadata."""


@dataclass(frozen=True, slots=True)
class ChromeRequest:
    """The headers of one request as Chrome sends them, and what curl needs to send them so.

    Attributes:
        headers: The headers, in order, but those curl makes itself: `Host`,
            `Content-Length`, and `Cookie`.
        order: Every header name, curl's included, in order, lower-cased and
            comma-separated, as `CURLOPT_HTTPHEADER_ORDER` takes them.
        weight: The HTTP/2 stream weight, which Chrome makes of `Priority`.
        credentials: Whether cookies go with it, and are taken from its response:
            not for a `fetch()` that has gone to another origin than its page's,
            as `fetch()` sends them to its own only, even once back.
    """

    headers: list[tuple[str, str]]
    order: str
    weight: int
    credentials: bool


def chrome_request(
    kind: RequestKind,
    *,
    chain: Sequence[str],
    method: str,
    own: Iterable[tuple[str, str]],
    referrer: str | None,
    client: Iterable[tuple[str, str]],
) -> ChromeRequest:
    """Make the headers Chrome sends for one hop of a request.

    Args:
        kind: The kind of request; a form submission redirected to a GET is a
            navigation.
        chain: The URLs of the redirect chain so far, the hop's last.
        method: The hop's method.
        own: The Request's own headers for the hop, but `Cookie`.
        referrer: The page the request comes from, as the Request's `Referer`
            names it; `None` for none.
        client: The headers that name the client: `sec-ch-ua`, `sec-ch-ua-mobile`,
            `sec-ch-ua-platform`, `User-Agent`, and `Accept-Language`.

    Returns:
        The headers and how curl sends them.
    """
    url = chain[-1]
    initiator = _origin(referrer) if referrer else None
    if initiator is None and kind != "navigation":
        initiator = _origin(chain[0])  # a page of its own origin, sending no Referer
    if kind == "form" and method == "GET":
        kind = "navigation"  # a form submission redirected to a GET
    shape = _SHAPES[kind]
    site = _chain_site(initiator, chain)
    values: dict[str, str] = {name.lower(): value for name, value in client}
    values |= {
        "connection": "keep-alive",
        "accept": shape.accept,
        "sec-fetch-site": site,
        "sec-fetch-mode": shape.mode,
        "sec-fetch-dest": shape.destination,
        "accept-encoding": "gzip, deflate, br, zstd",
        "priority": shape.priority,
    }
    document = kind in ("navigation", "form", "iframe")
    if document:
        values["upgrade-insecure-requests"] = "1"
    if kind in ("navigation", "form"):
        values["sec-fetch-user"] = "?1"
    if kind == "form":
        values["cache-control"] = "max-age=0"
    if site == "cross-site" and kind in ("iframe", "style", "script", "image"):
        values["sec-fetch-storage-access"] = "active"
    cors = kind == "fetch" and any(_origin(hop) != initiator for hop in chain)
    origin = _sent_origin(kind, method, initiator, chain, cors=cors)
    if origin is not None:
        values["origin"] = origin
    sent_referrer = _chain_referrer(referrer, chain)
    if sent_referrer is not None:
        values["referer"] = sent_referrer
    if not _trustworthy(url):
        for name in _SECURE_ONLY:
            values.pop(name, None)
        values["accept-encoding"] = "gzip, deflate"
    return _arranged(shape, values, own, document=document, credentials=not cors)


def site_relation(initiator: str | None, url: str) -> str:
    """Tell how a URL relates to the origin of the page that requests it, as `Sec-Fetch-Site` does.

    Args:
        initiator: The page's origin, such as `https://shop.example`; `None` for a
            request no page makes, such as a URL typed in.
        url: The URL requested.

    Returns:
        `none`, `same-origin`, `same-site` (the same scheme and registrable domain),
        or `cross-site`.
    """
    if initiator is None:
        return "none"
    if _origin(url) == initiator:
        return "same-origin"
    if _site(initiator) == _site(url):
        return "same-site"
    return "cross-site"


def _arranged(
    shape: _Shape, values: dict[str, str], own: Iterable[tuple[str, str]], *, document: bool, credentials: bool
) -> ChromeRequest:
    """Put the kind's headers and the Request's own in Chrome's order."""
    names = {name.lower() for name in shape.order}
    replaced: dict[str, list[tuple[str, str]]] = {}
    added: list[tuple[str, str]] = []
    for name, value in own:
        key = name.lower()
        if key in _DERIVED:
            continue
        if key in names:
            replaced.setdefault(key, []).append((name, value))
        else:
            added.append((name, value))
    arranged: list[tuple[str, str]] = []
    order: list[str] = []
    for name in shape.order:
        key = name.lower()
        if not document and key == _PAGE_BLOCK:
            arranged += added
            order += [added_name.lower() for added_name, _ in added]
        order.append(key)
        if key in replaced:
            arranged += replaced[key]
        elif key in values:
            arranged.append((name, values[key]))
    if document:
        arranged += added
        order += [added_name.lower() for added_name, _ in added]
    return ChromeRequest(arranged, ",".join(dict.fromkeys(order)), shape.weight, credentials)


def _chain_site(initiator: str | None, chain: Sequence[str]) -> str:
    """The `Sec-Fetch-Site` of a hop: the furthest relation of any URL of the chain so far."""
    rank = ("none", "same-origin", "same-site", "cross-site")
    return max((site_relation(initiator, url) for url in chain), key=rank.index)


def _sent_origin(
    kind: RequestKind, method: str, initiator: str | None, chain: Sequence[str], *, cors: bool
) -> str | None:
    """The `Origin` Chrome sends, as the Fetch standard serializes it.

    A form sends its page's origin, and a `fetch()` too, unless it is a GET or a
    HEAD that never left its page's origin. It is `null` once a redirect led from
    another origin than the page's to yet another, and for a form from an HTTPS
    page to another scheme, by `strict-origin-when-cross-origin`.

    Args:
        kind: The kind of request.
        method: The hop's method.
        initiator: The page's origin.
        chain: The URLs of the redirect chain so far.
        cors: Whether a `fetch()` has gone to another origin than its page's.
    """
    if initiator is None or kind not in ("form", "fetch"):
        return None
    if method in ("GET", "HEAD") and not cors:
        return None
    for current, location in itertools.pairwise(chain):
        if _origin(current) != _origin(location) and initiator != _origin(current):
            return "null"
    if kind == "form" and initiator.startswith("https:") and not chain[-1].lower().startswith("https:"):
        return "null"
    return initiator


def _chain_referrer(referrer: str | None, chain: Sequence[str]) -> str | None:
    """The `Referer` of the last hop of a chain: each hop's made of the one before, as a redirect keeps it."""
    for url in chain:
        if referrer is None:
            return None
        referrer = _sent_referrer(referrer, url)
    return referrer


def _sent_referrer(referrer: str, url: str) -> str | None:
    """The `Referer` Chrome sends by `strict-origin-when-cross-origin`: the whole page, its origin, or none.

    The page is sent whole to its own origin, without its fragment and credentials;
    its origin alone, with a slash, to another; and nothing from a potentially
    trustworthy page to one that is not.
    """
    try:
        page = URL(referrer)
    except ValueError:
        return None
    if page.protocol not in ("http:", "https:"):
        return None
    if _trustworthy(referrer) and not _trustworthy(url):
        return None
    if _origin(referrer) == _origin(url):
        page.hash = ""
        page.username = ""
        page.password = ""
        return page.href
    return f"{page.origin}/"


def _origin(url: str) -> str | None:
    try:
        origin = URL(url).origin
    except ValueError:
        return None
    return None if origin == "null" else origin


def _site(url_or_origin: str) -> tuple[str, str]:
    """The scheme and registrable domain of a URL, or its host where it has none, such as an address."""
    try:
        scheme = URL(url_or_origin).protocol
    except ValueError:
        scheme = ""
    host = url_host(url_or_origin) or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return scheme, _public_suffixes().privatesuffix(host) or host
    return scheme, host


def _trustworthy(url: str) -> bool:
    """Whether a URL's origin is potentially trustworthy: HTTPS, or a loopback host."""
    if url[:8].lower() == "https://":
        return True
    host = url_host(url)
    return host is not None and is_loopback_host(host)


@cache
def _public_suffixes() -> PublicSuffixList:
    """The Public Suffix List, loaded on first use."""
    return PublicSuffixList()
