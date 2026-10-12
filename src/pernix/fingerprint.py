"""Fingerprints: the keys that decide whether two Requests are duplicates.

The default Fingerprint covers the method, the normalized URL, the body, and the
headers declared for it, never cookies (ADR 0011), nor the Fetcher or the Session.
URLs are compared after `pernix.urls.normalize_url()`.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import orjson

from pernix.urls import normalize_url

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.request import Request

__all__ = ["FINGERPRINT_VERSION", "request_fingerprint"]

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
