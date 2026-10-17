from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from types import UnionType
from typing import Annotated, Any, TypeAliasType, Union, cast, get_args, get_origin, is_typeddict

import msgspec
import orjson

from pernix.secret import Secret

__all__ = ["convert_value"]

_CONTAINERS: tuple[object, ...] = (dict, list, tuple, set, frozenset, Mapping, Sequence)
_FROM_STRING: tuple[type, ...] = (Secret, PurePath)
"""Types without native msgspec support that may be given as a string."""


def convert_value(value: object, target: Any, *, lenient: bool) -> Any:
    """Validate a value against a type, converting it from text when lenient.

    Strict conversion accepts only values of the type itself, such as an `int` for an
    `int`. Lenient conversion, meant for text from the environment or a command line,
    also converts strings: `"3"` to `3`, `"true"` to `True`, and JSON to containers,
    dataclasses, Structs, and TypedDicts.

    Args:
        value: The value to convert.
        target: The type to convert to.
        lenient: Whether to convert strings.

    Returns:
        The converted value.

    Raises:
        msgspec.ValidationError: The value does not fit the type, or a string for a
            container type is not valid JSON.
    """
    if lenient and isinstance(value, str) and _is_container(target):
        try:
            value = orjson.loads(value)
        except orjson.JSONDecodeError as exc:
            raise msgspec.ValidationError(f"invalid JSON: {exc}") from exc
    return msgspec.convert(value, target, strict=not lenient, dec_hook=_decode_custom)


def _decode_custom(target: object, value: object) -> object:
    """Validate a value whose declared type msgspec does not handle natively.

    An instance of the declared type is kept as is. A string is converted only for the
    types in `_FROM_STRING`; other types are never built from arbitrary input.
    """
    if isinstance(target, type):
        if isinstance(value, target):
            return value
        if isinstance(value, str) and issubclass(target, _FROM_STRING):
            return target(value)
    raise TypeError(f"Expected `{getattr(target, '__name__', target)}`, got `{type(value).__name__}`")


def _is_container(target: Any) -> bool:
    if isinstance(target, TypeAliasType):
        return _is_container(target.__value__)
    origin = get_origin(target)
    if origin is Annotated:
        return _is_container(get_args(target)[0])
    if origin in (Union, UnionType):
        return any(_is_container(member) for member in get_args(target))
    if isinstance(target, type) and _is_record(cast("type[Any]", target)):
        return True
    return (origin or target) in _CONTAINERS


def _is_record(target: type[Any]) -> bool:
    """Whether a type is converted from a mapping: a Struct, a dataclass, or a TypedDict."""
    return issubclass(target, msgspec.Struct) or dataclasses.is_dataclass(target) or is_typeddict(target)
