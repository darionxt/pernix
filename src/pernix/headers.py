from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import cast

__all__ = ["Headers", "HeadersInput"]

type HeadersInput = Headers | Mapping[str, str] | Iterable[tuple[str, str]] | None
"""Anything `Headers` accepts: another `Headers`, a mapping, or `(name, value)` pairs."""


class Headers:
    """Immutable HTTP headers that keep wire order and case, and allow repeated names.

    Lookups by name are case-insensitive. Iterating yields `(name, value)` pairs in
    wire order.

    Example:
        >>> headers = Headers([("Set-Cookie", "a=1"), ("set-cookie", "b=2")])
        >>> headers.get_all("SET-COOKIE")
        ['a=1', 'b=2']
    """

    __slots__ = ("_items",)

    _items: tuple[tuple[str, str], ...]

    def __init__(self, headers: HeadersInput = None) -> None:
        """Create headers from another `Headers`, a mapping, or `(name, value)` pairs.

        Args:
            headers: The headers to copy. `None` creates empty headers.
        """
        if headers is None:
            self._items = ()
        elif isinstance(headers, Headers):
            self._items = headers._items
        elif isinstance(headers, Mapping):
            mapping = cast("Mapping[str, str]", headers)
            self._items = tuple((str(k), str(v)) for k, v in mapping.items())
        else:
            self._items = tuple((str(k), str(v)) for k, v in headers)

    def get(self, name: str, default: str | None = None) -> str | None:
        """Return the first value of a header.

        Args:
            name: The header name, matched case-insensitively.
            default: The value to return when the header is absent.

        Returns:
            The first value in wire order, or `default`.
        """
        lowered = name.lower()
        return next((v for k, v in self._items if k.lower() == lowered), default)

    def get_all(self, name: str) -> list[str]:
        """Return every value of a header, in wire order.

        Args:
            name: The header name, matched case-insensitively.

        Returns:
            All values of the header; empty if it is absent.
        """
        lowered = name.lower()
        return [v for k, v in self._items if k.lower() == lowered]

    def items(self) -> list[tuple[str, str]]:
        """Return all `(name, value)` pairs in wire order."""
        return list(self._items)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        lowered = name.lower()
        return any(k.lower() == lowered for k, _ in self._items)

    def __iter__(self) -> Iterator[tuple[str, str]]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Headers):
            return NotImplemented
        return self._items == other._items

    def __hash__(self) -> int:
        return hash(self._items)

    def __repr__(self) -> str:
        return f"Headers({list(self._items)!r})"
