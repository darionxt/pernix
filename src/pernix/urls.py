"""URL clean-up, joining, normalization, and hosts, shared by Requests, Responses, Fingerprints, and Fetchers."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urljoin, urlsplit, uses_relative

from ada_url import URL

__all__ = [
    "clean_url",
    "document_base",
    "is_absolute",
    "is_loopback_host",
    "join_url",
    "normalize_url",
    "url_host",
    "with_ascii_host",
]

_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x21))
_TAB_OR_NEWLINE = str.maketrans("", "", "\t\n\r")

_AUTHORITY = re.compile(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//(?P<authority>[^/?#]*)")
"""The authority of a URL, as curl and urllib end it: at the first `/`, `?`, or `#`."""


def clean_url(url: str) -> str:
    """Remove what browsers ignore in a URL before parsing it.

    Leading and trailing C0 control characters and spaces are stripped, and tabs and
    newlines anywhere are removed, as in `href` values that wrap across lines.

    Args:
        url: The URL as written.

    Returns:
        The cleaned URL.
    """
    return url.strip(_C0_CONTROL_OR_SPACE).translate(_TAB_OR_NEWLINE)


def is_absolute(url: str) -> bool:
    """Whether a URL has a scheme, such as `https:`; `//host/path` and `/path` do not.

    Args:
        url: A cleaned URL.

    Returns:
        `True` if the URL can be used without a base URL.
    """
    return bool(urlsplit(url).scheme)


def join_url(base: str, url: str) -> str:
    """Resolve a URL against an absolute base URL, after cleaning it.

    Args:
        base: The absolute base URL.
        url: An absolute or relative URL.

    Returns:
        The absolute URL.
    """
    return urljoin(base, clean_url(url))


def document_base(url: str, href: str | None) -> str:
    """Return the base URL of a document, as browsers choose it.

    A `<base href>` is resolved against the document URL. It is ignored when it
    cannot be parsed or is not a URL that others can be resolved against, such as a
    `data:`, `javascript:`, or `about:` URL.

    Args:
        url: The absolute URL of the document.
        href: The `href` of the document's first `<base>` with one, if any.

    Returns:
        The base URL to resolve the document's links against.
    """
    if href is None:
        return url
    try:
        base = join_url(url, href)
        scheme = urlsplit(base).scheme
    except ValueError:
        return url
    return base if scheme and scheme in uses_relative else url


def normalize_url(url: str, *, sort_query: bool = True) -> str:
    """Normalize a URL for comparison, such as in Fingerprints.

    It does three things only: serialize the URL by WHATWG rules, drop the fragment,
    and sort the query parameters stably by name. Percent-encoded bytes are kept as
    they are.

    Args:
        url: An absolute URL.
        sort_query: Sort the query parameters stably by name.

    Returns:
        The URL serialized by WHATWG rules, without its fragment, with sorted query
        parameters; a URL WHATWG rules cannot parse is returned as it is.
    """
    try:
        parsed = URL(url)
    except ValueError:
        return url
    parsed.hash = ""
    if sort_query and parsed.search:
        parameters = parsed.search.removeprefix("?").split("&")
        parsed.search = "&".join(sorted(parameters, key=lambda parameter: parameter.partition("=")[0]))
    return parsed.href


def is_loopback_host(host: str) -> bool:
    """Whether a host name is a loopback one: a loopback IP address, `localhost`, or a `.localhost` name.

    Args:
        host: The host, as in a URL; IPv6 addresses may be bracketed.

    Returns:
        Whether connecting to it stays on this machine.
    """
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def url_host(url: str) -> str | None:
    """Return the host a client connects to for a URL: lower-cased, IPv6 without brackets, and in ASCII.

    The host is the one curl connects to: after the last `@` of the authority, even
    where WHATWG URLs would end the authority earlier, such as at a backslash. One
    that is not ASCII, or holds percent escapes, is converted as WHATWG URLs convert
    hosts: percent-decoded, without the transitional IDNA mappings, so `faß` is
    `xn--fa-hia`, not `fass`, and with IPv4 addresses in dotted decimal, whatever
    digits they were written in.

    Args:
        url: The URL.

    Returns:
        The host, or `None` for none, or for one that WHATWG URLs reject.
    """
    url = clean_url(url)
    span = _host_span(url)
    if span is None:
        return None
    host = url[span[0] : span[1]]
    if host.startswith("["):
        return host[1:-1].lower() or None  # an IPv6 address
    return _whatwg_host(host) if _converted(host) else host.lower() or None


def with_ascii_host(url: str) -> str:
    """Return a URL with the host `url_host()` names in place of one that is not ASCII or holds percent escapes.

    Clients that convert hosts themselves, such as curl, then reach the host a
    browser does, whatever IDNA conversion their platform has. The rest of the URL is
    kept as it is.

    Args:
        url: A cleaned URL.

    Returns:
        The URL, with its host converted if needed.

    Raises:
        ValueError: WHATWG URLs reject the host.
    """
    span = _host_span(url)
    if span is None:
        return url
    start, end = span
    host = url[start:end]
    if host.startswith("[") or not _converted(host):
        return url
    converted = _whatwg_host(host)
    if converted is None:
        raise ValueError(f"the host of {url} is not valid: WHATWG URLs reject it, as browsers do")
    return url[:start] + converted + url[end:]


def _host_span(url: str) -> tuple[int, int] | None:
    """Where the host curl connects to is in a cleaned URL: after the last `@` of the authority, without the port."""
    match = _AUTHORITY.match(url)
    if match is None:
        return None
    authority = match["authority"]
    at = authority.rfind("@") + 1
    host_and_port = authority[at:]
    if host_and_port.startswith("["):
        length = host_and_port.find("]") + 1
        if not length:
            return None  # an unclosed IPv6 bracket
    else:
        length = len(host_and_port.partition(":")[0])
    start = match.start("authority") + at
    return start, start + length


def _converted(host: str) -> bool:
    """Whether a host is converted before use: one that is not ASCII, or holds percent escapes."""
    return not host.isascii() or "%" in host


def _whatwg_host(host: str) -> str | None:
    """A host as WHATWG URLs convert it; `None` for one they reject."""
    if "\\" in host:
        return None  # WHATWG URLs would end the host there
    try:
        return URL(f"http://{host}/").hostname
    except ValueError:
        return None
