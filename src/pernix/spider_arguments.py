from __future__ import annotations

import inspect
import sys
from typing import TYPE_CHECKING, Any

import msgspec

from pernix.components import init_type_hints
from pernix.errors import SpiderArgumentError
from pernix.settings.conversion import convert_value
from pernix.settings.suggestions import did_you_mean

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.spider import Spider

__all__ = ["RESERVED_ARGUMENTS", "check_argument_names", "create_spider", "spider_arguments"]

RESERVED_ARGUMENTS = frozenset({"settings", "output"})
"""Keyword arguments of the run entry points, which no Spider argument may use."""

_BY_NAME = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def check_argument_names(spider_type: type[Spider]) -> None:
    """Fail when a Spider's `__init__` takes a parameter that cannot be a Spider argument.

    Args:
        spider_type: The Spider class.

    Raises:
        TypeError: A parameter is named `settings` or `output`, which the run entry
            points reserve, or is required and positional-only, since Spider
            arguments are given by name.
    """
    parameters = _parameters(spider_type)
    clashes = sorted(RESERVED_ARGUMENTS & parameters.keys())
    if clashes:
        names = " and ".join(repr(name) for name in clashes)
        raise TypeError(
            f"{spider_type.__qualname__}.__init__ takes {names}, which run(), crawl(), and stream_items() "
            "reserve for themselves; rename the Spider argument"
        )
    for name, parameter in parameters.items():
        if parameter.kind is inspect.Parameter.POSITIONAL_ONLY and parameter.default is inspect.Parameter.empty:
            raise TypeError(
                f"{spider_type.__qualname__}.__init__ requires {name!r} as positional-only, "
                "but Spider arguments are given by name"
            )


def spider_arguments(spider_type: type[Spider], args: Mapping[str, object]) -> dict[str, Any]:
    """Check Spider arguments against the parameters of the Spider's `__init__`, and convert them.

    Arguments are passed by name. A string is converted to the parameter's annotated
    type as settings from the environment are: `"3"` to `3`, `"true"` to `True`, and
    JSON to lists, dicts, dataclasses, and Structs. Any other value must already have
    that type. A parameter without an annotation takes the value as is, and so does
    `**kwargs` for arguments no parameter names.

    Args:
        spider_type: The Spider class.
        args: The arguments, by name.

    Returns:
        The converted arguments.

    Raises:
        pernix.errors.SpiderArgumentError: Arguments are missing, unknown, or do not
            fit their types; every problem is listed.
    """
    parameters = _parameters(spider_type)
    by_name = {name: parameter for name, parameter in parameters.items() if parameter.kind in _BY_NAME}
    takes_any = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    hints = init_type_hints(spider_type)
    problems: list[str] = []
    converted: dict[str, Any] = {}
    for name, value in args.items():
        if name not in by_name:
            if takes_any:
                converted[name] = value
            elif by_name:
                problems.append(f"unknown argument {name!r}{did_you_mean(name, by_name)}")
            else:
                problems.append(f"unknown argument {name!r}: {spider_type.__qualname__} takes no arguments")
            continue
        if name not in hints:
            converted[name] = value
            continue
        try:
            converted[name] = convert_value(value, hints[name], lenient=isinstance(value, str))
        except msgspec.ValidationError as exc:
            problems.append(f"{name}: {exc}")
    for name, parameter in by_name.items():
        if parameter.default is inspect.Parameter.empty and name not in args:
            problems.append(f"missing argument {name!r}")
    if problems:
        raise SpiderArgumentError(problems)
    return converted


def _parameters(spider_type: type[Spider]) -> Mapping[str, inspect.Parameter]:
    """The parameters of a Spider's `__init__`; from Python 3.14 on, names not defined yet in annotations do not raise."""
    if sys.version_info >= (3, 14):
        import annotationlib  # new in 3.14

        return inspect.signature(spider_type, annotation_format=annotationlib.Format.FORWARDREF).parameters
    return inspect.signature(spider_type).parameters


def create_spider(spider_type: type[Spider], args: Mapping[str, object]) -> Spider:
    """Create a Spider object from its arguments.

    The arguments are converted with `spider_arguments()`. A `ValueError` raised by
    the Spider's `__init__` rejects them too, so a Spider can check its arguments
    further.

    Args:
        spider_type: The Spider class.
        args: The arguments, by name.

    Returns:
        The Spider.

    Raises:
        pernix.errors.SpiderArgumentError: The arguments are invalid.
    """
    converted = spider_arguments(spider_type, args)
    try:
        return spider_type(**converted)
    except ValueError as exc:
        raise SpiderArgumentError([f"{spider_type.__qualname__}: {exc}"]) from exc
