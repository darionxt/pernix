"""Proxy records: a proxy as its source gives it, with the tags binding policies select it by."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import msgspec

from pernix.proxies.proxy_url import proxy_address

if TYPE_CHECKING:
    from collections.abc import Collection

__all__ = ["Proxy"]


class Proxy(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    """A proxy as its source gives it, with the tags binding policies select it by, and when it expires.

    Sources give proxies as URLs, or as `Proxy` records to tag them: a provider
    returns either, and `proxies.urls` takes either, a record as a table such as
    `{ url = "http://proxy:8080", tags = ["us"] }`. The URL is checked when the
    proxy is loaded, as `pernix.proxies.proxy_url()` takes it. Its `repr` shows it
    without its credentials.

    Attributes:
        url: The proxy URL, such as `http://user:password@proxy:8080`, or
            `proxy:8080` for HTTP.
        tags: Tags a binding policy may select it by, such as its region; any
            collection of strings given is held as a `frozenset`.
        expires: When it stops working, as its source tells, in seconds since the
            epoch; `None` if it does not tell. Binding policies may read it; the
            pool does not act on it yet.

    Example:
        >>> Proxy("http://user:pw@gate.example:7777", tags=frozenset({"us"}))
        Proxy('http://***@gate.example:7777', tags=frozenset({'us'}))
    """

    url: str
    tags: frozenset[str] = frozenset()
    expires: float | None = None

    def __post_init__(self) -> None:
        tags = cast("Collection[str]", self.tags)  # a list or a set given in code too
        if not isinstance(tags, frozenset):
            msgspec.structs.force_setattr(self, "tags", frozenset(tags))

    def __repr__(self) -> str:
        parts = [repr(proxy_address(self.url))]
        if self.tags:
            parts.append(f"tags={self.tags!r}")
        if self.expires is not None:
            parts.append(f"expires={self.expires!r}")
        return f"Proxy({', '.join(parts)})"
