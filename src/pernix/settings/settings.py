from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from pernix.settings.suggestions import did_you_mean

if TYPE_CHECKING:
    from pernix.settings.section import Section

__all__ = ["Settings"]


class Settings(Mapping[str, Any]):
    """Validated, immutable settings for one Crawler.

    Values are looked up by dotted key, or as a typed Section. Settings cannot be
    modified once created; every Crawler has its own instance.

    Example:
        >>> settings["concurrency"]
        16
        >>> settings.section(CoreSettings).concurrency
        16
    """

    __slots__ = ("_origins", "_sections", "_values")

    def __init__(
        self,
        values: Mapping[str, Any],
        *,
        origins: Mapping[str, str],
        sections: Mapping[type[Section], Section],
    ) -> None:
        """Wrap validated values. Use `load_settings()` to create settings.

        Args:
            values: Every known key with its validated value, defaults included.
            origins: For each key, a description of where its value came from.
            sections: Every registered Section type with its validated instance.
        """
        self._values: Mapping[str, Any] = MappingProxyType(dict(values))
        self._origins: Mapping[str, str] = MappingProxyType(dict(origins))
        self._sections: Mapping[type[Section], Section] = MappingProxyType(dict(sections))

    def __getitem__(self, key: str) -> Any:
        try:
            return self._values[key]
        except KeyError:
            raise KeyError(f"unknown setting {key!r}{did_you_mean(key, self._values)}") from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def section[S: Section](self, section_type: type[S]) -> S:
        """Return the validated values of a Section.

        Args:
            section_type: A Section registered with these settings.

        Returns:
            The Section instance.

        Raises:
            KeyError: The Section is not registered.
        """
        try:
            return cast("S", self._sections[section_type])
        except KeyError:
            raise KeyError(f"{section_type.__qualname__} is not a registered settings section") from None

    def sections(self) -> list[type[Section]]:
        """Return every registered Section type."""
        return list(self._sections)

    def components(self, key: str) -> list[object]:
        """Return the enabled components of a component map, in order.

        Args:
            key: The key of a `ComponentMap` setting.

        Returns:
            The resolved components with a non-`None` order, lowest order first; ties
            keep registration order. They are not constructed.

        Raises:
            KeyError: The key is unknown.
            TypeError: The setting is not a component map.
        """
        registry = self[key]
        if not isinstance(registry, Mapping):
            raise TypeError(f"setting {key!r} is not a component map")
        entries = cast("Mapping[object, int | None]", registry).items()
        enabled = [(order, component) for component, order in entries if order is not None]
        return [component for _, component in sorted(enabled, key=lambda entry: entry[0])]

    def named_components(self, key: str) -> dict[str, object]:
        """Return the enabled components of a `NamedComponents` registry, by name.

        Args:
            key: The key of a `NamedComponents` setting.

        Returns:
            The resolved components that are not disabled, in registration order.
            They are not constructed.

        Raises:
            KeyError: The key is unknown.
            TypeError: The setting is not a registry.
        """
        registry = self[key]
        if not isinstance(registry, Mapping):
            raise TypeError(f"setting {key!r} is not a registry of named components")
        entries = cast("Mapping[str, object | None]", registry).items()
        return {name: component for name, component in entries if component is not None}

    def origin(self, key: str) -> str:
        """Describe where the value of a setting came from.

        Args:
            key: A known setting key.

        Returns:
            For example `"Spider.settings"` or `"environment variable PERNIX_CONCURRENCY"`.
        """
        self[key]  # raises KeyError with a suggestion for unknown keys
        return self._origins.get(key, "built-in default")

    def __repr__(self) -> str:
        return f"Settings({dict(self._values)!r})"
