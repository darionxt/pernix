"""URL clean-up and joining shared by Requests and Responses."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit, uses_relative

__all__ = ["clean_url", "document_base", "is_absolute", "join_url"]

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
