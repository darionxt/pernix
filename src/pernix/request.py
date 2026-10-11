from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pernix.headers import Headers, HeadersInput

if TYPE_CHECKING:
    from pernix.response import Response

__all__ = ["Callback", "Request"]

type Callback = Callable[[Response], AsyncIterator[Any] | Awaitable[Any]]
"""A Spider method that receives a Response and produces Requests and Items."""


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Request:
    """An immutable description of one fetch and the Callback that receives its Response.

    Derive modified copies with `replace()`.

    Attributes:
        url: The absolute URL to fetch.
        method: The HTTP method, upper-cased.
        headers: The request headers, in wire order.
        body: The request body, if any.
        callback: The Spider method that receives the Response; `None` means `parse`.
        meta: Read-only user data carried to the Response.
    """

    url: str
    method: str
    headers: Headers
    body: bytes | None
    callback: Callback | None
    meta: Mapping[str, Any]

    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: HeadersInput = None,
        body: bytes | str | None = None,
        callback: Callback | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Create a Request.

        Args:
            url: The absolute URL to fetch.
            method: The HTTP method; any case is accepted.
            headers: Request headers as a mapping or as `(name, value)` pairs.
            body: The request body; a `str` is encoded as UTF-8.
            callback: The Spider method that receives the Response. Defaults to `parse`.
            meta: User data carried to the Response. It is copied and made read-only.
        """
        set_field = object.__setattr__
        set_field(self, "url", url)
        set_field(self, "method", method.upper())
        set_field(self, "headers", Headers(headers))
        set_field(self, "body", body.encode() if isinstance(body, str) else body)
        set_field(self, "callback", callback)
        set_field(self, "meta", MappingProxyType(dict(meta or {})))

    def replace(self, **changes: Any) -> Request:
        """Return a copy of this Request with some fields replaced.

        Args:
            **changes: New values, keyed by the `__init__` parameter names.

        Returns:
            The new Request; this one is unchanged.
        """
        return dataclasses.replace(self, **changes)

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url}>"
