"""Telling the Spider hooks a subclass overrides from those it inherits from the base class."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["base_hook", "overrides"]

_BASE = "__pernix_base_hook__"


def base_hook[F: Callable[..., object]](method: F) -> F:
    """Mark the base implementation of a Spider hook, which a subclass overrides to take part.

    Args:
        method: The method of the base class.

    Returns:
        The same method, marked.
    """
    setattr(method, _BASE, True)
    return method


def overrides(method: object) -> bool:
    """Whether a Spider's hook is its own, rather than the base implementation marked with `base_hook()`.

    Args:
        method: The hook, as an attribute of the Spider.

    Returns:
        Whether the Spider's class, or one between it and the base class, defines it.
    """
    return not getattr(method, _BASE, False)
