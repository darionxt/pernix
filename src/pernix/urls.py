"""URL clean-up, joining, and normalization shared by Requests, Responses, and Fingerprints."""

from __future__ import annotations

import ipaddress
from urllib.parse import urljoin, urlsplit, uses_relative

from ada_url import URL

__all__ = ["clean_url", "document_base", "is_absolute", "is_loopback_host", "join_url", "normalize_url"]

_C0_CONTROL_OR_SPACE = "".join(chr(code) for code in range(0x21))
_TAB_OR_NEWLINE = str.maketrans("", "", "\t\n\r")


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
