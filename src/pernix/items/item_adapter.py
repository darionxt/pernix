from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable, Iterable, Mapping
from typing import Any, cast

import msgspec

from pernix.components import resolve_component
from pernix.request import Request
from pernix.response import Response

__all__ = ["ItemFilter", "is_item", "item_matcher", "item_to_dict"]

type ItemFilter = tuple[type | str, ...] | list[type | str] | Callable[[Any], bool]
"""Which Items a Pipeline or a Feed takes: types, as classes or import paths, or a function `(item) -> bool`."""

_NOT_ITEMS = (Request, Response)


def is_item(value: object) -> bool:
    """Tell whether a Callback output is an Item.

    Items are mappings, dataclass instances, msgspec `Struct`s, pydantic models, and
    attrs instances; `Request` and `Response` are not. pydantic and attrs are only
    consulted when the application imported them.

    Args:
        value: A Callback output.

    Returns:
        Whether it is an Item.
    """
    if isinstance(value, Mapping | msgspec.Struct):
        return True
    if isinstance(value, (type, *_NOT_ITEMS)):
        return False
    return dataclasses.is_dataclass(value) or _is_pydantic_model(value) or _is_attrs_instance(value)


def item_to_dict(item: object) -> dict[str, Any]:
    """Convert an Item to a plain dict, for export.

    Nested Items become dicts, and lists, tuples, and sets become lists; other values
    are kept. Dataclasses and attrs instances give their fields, msgspec `Struct`s
    their encoded field names (honoring `rename`), and pydantic models
    `model_dump()`, which uses field names rather than aliases.

    Args:
        item: An Item, as `is_item()` tells.

    Returns:
        A new dict.

    Raises:
        TypeError: `item` is not an Item.
    """
    if not is_item(item):
        raise TypeError(f"{item!r} is not an Item")
    return cast("dict[str, Any]", _plain(item))


def item_matcher(accepts: ItemFilter | None) -> Callable[[object], bool]:
    """Turn the `accepts` of a Pipeline or a Feed into a test of Items.

    Args:
        accepts: Types, as classes or import paths; a function `(item) -> bool`; or
            `None` for every Item.

    Returns:
        A function telling whether an Item is accepted.

    Raises:
        TypeError: `accepts` is none of these.
        pernix.errors.ComponentError: An import path cannot be loaded.
    """
    if accepts is None:
        return lambda _item: True
    if isinstance(accepts, tuple | list):
        types = tuple(resolve_component(entry) for entry in cast("Iterable[object]", accepts))
        if not all(isinstance(entry, type) for entry in types):
            raise TypeError(f"accepts must list types, got {accepts!r}")
        accepted = cast("tuple[type, ...]", types)
        return lambda item: isinstance(item, accepted)
    if callable(accepts) and not isinstance(accepts, type):
        return accepts
    raise TypeError(f"accepts must be a tuple of types or a function (item) -> bool, got {accepts!r}")


def _plain(value: object) -> object:
    if isinstance(value, str | bytes | int | float | bool | None):
        return value
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in cast("Mapping[object, object]", value).items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in cast("list[object]", value)]
    if isinstance(value, msgspec.Struct):
        return {field.encode_name: _plain(getattr(value, field.name)) for field in msgspec.structs.fields(value)}
    if isinstance(value, (type, *_NOT_ITEMS)):
        return value
    if dataclasses.is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if _is_pydantic_model(value):
        model: Any = value
        return _plain(model.model_dump())
    if _is_attrs_instance(value):
        attr: Any = sys.modules["attr"]
        return {field.name: _plain(getattr(value, field.name)) for field in attr.fields(type(value))}
    return value


def _is_pydantic_model(value: object) -> bool:
    pydantic = sys.modules.get("pydantic")
    model: Any = getattr(pydantic, "BaseModel", None)
    return model is not None and isinstance(value, model)


def _is_attrs_instance(value: object) -> bool:
    attr: Any = sys.modules.get("attr")
    return attr is not None and bool(attr.has(type(value)))
