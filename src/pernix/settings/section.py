from __future__ import annotations

from typing import Any, ClassVar, dataclass_transform

import msgspec

__all__ = ["ComponentMap", "Section"]

type ComponentMap = dict[Any, int | None]
"""A registry of components and their order; `None` (or `false` in TOML) disables one.

A component is a class, an object, or an import path such as `"myproject.middlewares.Sign"`.
Lower orders come first.
"""


@dataclass_transform(frozen_default=True, kw_only_default=True, field_specifiers=(msgspec.field,))
class _SectionMeta(msgspec.StructMeta):
    """Makes every Section subclass frozen and keyword-only, and tells type checkers so."""

    def __new__(mcls, name: str, bases: tuple[type, ...], namespace: dict[str, Any], **kwargs: Any) -> _SectionMeta:
        kwargs.setdefault("frozen", True)
        kwargs.setdefault("kw_only", True)
        return super().__new__(mcls, name, bases, namespace, **kwargs)


class Section(msgspec.Struct, metaclass=_SectionMeta):
    """A typed, immutable group of settings that share a key prefix.

    Subclasses set `__prefix__` and declare each setting as a field with a default.
    The key of a field is `"<prefix>.<field>"`, or just `"<field>"` for the empty
    prefix. Constraints use `typing.Annotated` with `msgspec.Meta`. Sections are
    frozen and keyword-only; prefer immutable container types (`tuple`, `frozenset`)
    so values cannot be changed through the Section either, and use
    `msgspec.field(default_factory=...)` for mutable defaults.

    Example:
        >>> class RetrySettings(Section):
        ...     __prefix__ = "retry"
        ...     times: Annotated[int, msgspec.Meta(ge=0)] = 3
        ...
        >>> RetrySettings().times
        3
    """

    __prefix__: ClassVar[str] = ""

    @classmethod
    def key(cls, field: str) -> str:
        """Return the settings key of one of this section's fields.

        Args:
            field: The field name.

        Returns:
            The dotted settings key.
        """
        return f"{cls.__prefix__}.{field}" if cls.__prefix__ else field
