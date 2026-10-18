"""The kinds of resources a page loads, which rendered Requests may block."""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING, Literal, cast, get_args

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["RESOURCE_TYPES", "ResourceType", "check_resource_types"]

type ResourceType = Literal[
    "image",
    "media",
    "font",
    "stylesheet",
    "script",
    "xhr",
    "fetch",
    "eventsource",
    "texttrack",
    "manifest",
    "other",
]
"""A kind of resource the browser loads for a page, as the browser names it; `other` covers the kinds it names apart."""

RESOURCE_TYPES: frozenset[str] = frozenset(get_args(ResourceType.__value__))
"""The resource types that can be blocked."""

_UNBLOCKABLE = {
    "document": "the page itself is a document, as are its frames",
    "websocket": "the browser does not let a page's WebSockets be blocked by type",
}


def check_resource_types(types: str | Iterable[str]) -> tuple[ResourceType, ...]:
    """Check resource types to block, in the order given, without duplicates.

    Args:
        types: The resource types; a string is one type.

    Returns:
        The types, as a tuple.

    Raises:
        ValueError: A type is not one that can be blocked; the message suggests the
            closest one.
        TypeError: A type is not a string.
    """
    checked: list[ResourceType] = []
    names = cast("Iterable[object]", (types,) if isinstance(types, str) else types)  # annotations are not enforced
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"a resource type is a string, such as 'image'; got {name!r}")
        if name in _UNBLOCKABLE:
            raise ValueError(f"{name!r} cannot be blocked: {_UNBLOCKABLE[name]}")
        if name not in RESOURCE_TYPES:
            raise ValueError(f"{name!r} is not a resource type{_suggestion(name)}")
        if name not in checked:
            checked.append(cast("ResourceType", name))
    return tuple(checked)


def _suggestion(name: str) -> str:
    """The closest resource type to a misspelled one, or the list of them."""
    close = difflib.get_close_matches(name, sorted(RESOURCE_TYPES), n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else f"; the types are {', '.join(sorted(RESOURCE_TYPES))}"
