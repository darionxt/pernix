"""Fingerprints: the keys that decide whether two Requests are duplicates.

The default Fingerprint covers the method, the normalized URL, the body, and the
headers declared for it, never cookies (ADR 0011), nor the Fetcher or the Session.
URL normalization does three things only: serialize the URL by WHATWG rules, drop
the fragment, and sort the query parameters stably by name. Percent-encoded bytes
are kept as they are.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import orjson
from ada_url import URL

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.request import Request

__all__ = ["FINGERPRINT_VERSION", "normalize_url", "request_fingerprint"]

FINGERPRINT_VERSION = 1
"""Changes whenever the default Fingerprint of some Request changes."""


def request_fingerprint(request: Request, *, headers: Iterable[str] = (), sort_query: bool = True) -> str:
    """Compute the default Fingerprint of a Request.

    Custom Fingerprint functions can fall back to this one for the Requests they do
    not handle themselves.

    Args:
        request: The Request.
        headers: Names of the request headers whose values take part, matched
            case-insensitively.
        sort_query: Sort the query parameters by name, so their order does not matter.

    Returns:
        32 hexadecimal characters.

    Raises:
        ValueError: `headers` names the `Cookie` header.
    """
    names = sorted({name.strip().lower() for name in headers})
    if "cookie" in names:
        raise ValueError("cookies never take part in a Fingerprint (ADR 0011), so the Cookie header cannot be declared")
    canonical = orjson.dumps(
        [
            FINGERPRINT_VERSION,
            request.method,
            normalize_url(request.url, sort_query=sort_query),
            hashlib.blake2b(request.body or b"", digest_size=16).hexdigest(),
            [[name, request.headers.get_all(name)] for name in names],
        ]
    )
    return hashlib.blake2b(canonical, digest_size=16).hexdigest()


def normalize_url(url: str, *, sort_query: bool = True) -> str:
    """Normalize a URL for comparison.

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
