"""URL clean-up and joining shared by Requests and Responses."""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit

__all__ = ["clean_url", "is_absolute", "join_url"]

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
