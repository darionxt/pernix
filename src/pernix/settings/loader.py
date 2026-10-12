from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import MappingProxyType, UnionType
from typing import Annotated, Any, TypeAliasType, Union, cast, get_args, get_origin

import msgspec
import orjson
from msgspec import structs

from pernix.components import init_type_hints, resolve_component
from pernix.errors import ComponentError, SettingsError
from pernix.settings.core_settings import CORE_SECTIONS
from pernix.settings.secret import Secret
from pernix.settings.section import ComponentMap, NamedComponents, Section
from pernix.settings.settings import Settings
from pernix.settings.sources import find_project_file, read_environment
from pernix.settings.suggestions import did_you_mean

__all__ = ["load_settings"]

_CONTAINERS: tuple[object, ...] = (dict, list, tuple, set, frozenset, Mapping, Sequence)
_FROM_STRING: tuple[type, ...] = (Secret, PurePath)
"""Types without native msgspec support that settings may give as a string."""


def load_settings(
    *,
    sections: Iterable[type[Section]] = CORE_SECTIONS,
    addon_layers: Sequence[Mapping[str, Any]] = (),
    project_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    spider_settings: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
    ignore_unknown: bool = False,
) -> Settings:
    """Merge every settings layer, validate the result, and freeze it.

    Layers, from lowest to highest precedence: Section defaults, Addons, the project
    file, `PERNIX_*` environment variables, `Spider.settings`, and run arguments.
    A later layer replaces a value; for a component map it adds or overrides entries.
    Keys may be dotted (`"domain.delay"`) or nested (`{"domain": {"delay": 0.5}}`).

    Components listed in component maps may register further Sections, through
    Section-typed `__init__` parameters or a `settings_sections` attribute; their
    keys are validated together with the core keys.

    Args:
        sections: The Sections known up front.
        addon_layers: Settings contributed by Addons, in order.
        project_dir: Where to start looking for the project file; defaults to the
            working directory.
        environ: The environment to read `PERNIX_*` variables from; defaults to
            `os.environ`.
        spider_settings: The `settings` attribute of the Spider.
        overrides: Settings passed to `run()` or on the command line.
        ignore_unknown: Skip keys that no known Section declares instead of failing,
            to read a few settings before every Section is known.

    Returns:
        The validated, immutable settings.

    Raises:
        SettingsError: Some keys are unknown, some values have the wrong type, or a
            component cannot be loaded. Every problem found is reported at once.
    """
    sources = [_Source(layer, "Addon") for layer in addon_layers]
    project = find_project_file(Path.cwd() if project_dir is None else Path(project_dir))
    if project is not None:
        sources.append(_Source(project.values, project.origin))
    environment = read_environment(os.environ if environ is None else environ)
    sources.append(
        _Source(
            {key: value for key, (value, _) in environment.items()},
            "the environment",
            lenient=True,
            key_origins={key: f"environment variable {name}" for key, (_, name) in environment.items()},
        )
    )
    sources.append(_Source(spider_settings or {}, "Spider.settings"))
    sources.append(_Source(overrides or {}, "run() arguments"))
    return _Loader(sources, sections, ignore_unknown=ignore_unknown).load()


@dataclass(frozen=True, slots=True)
class _Source:
    """The raw settings of one layer."""

    values: Mapping[str, Any]
    origin: str
    lenient: bool = False
    key_origins: Mapping[str, str] = field(default_factory=dict[str, str])

    def describe(self, key: str) -> str:
        return self.key_origins.get(key, self.origin)


@dataclass(frozen=True, slots=True)
class _Field:
    section: type[Section]
    name: str
    type: Any
    default: Any

    @property
    def is_component_map(self) -> bool:
        return self.type is ComponentMap

    @property
    def is_named_components(self) -> bool:
        return self.type is NamedComponents

    @property
    def is_registry(self) -> bool:
        """Whether layers merge the value entry by entry instead of replacing it."""
        return self.is_component_map or self.is_named_components


@dataclass(slots=True)
class _Merged:
    values: dict[str, Any] = field(default_factory=dict[str, Any])
    origins: dict[str, str] = field(default_factory=dict[str, str])
    lenient: dict[str, bool] = field(default_factory=dict[str, bool])
    problems: list[str] = field(default_factory=list[str])


class _Loader:
    def __init__(self, sources: Sequence[_Source], sections: Iterable[type[Section]], *, ignore_unknown: bool) -> None:
        self._sources = sources
        self._ignore_unknown = ignore_unknown
        self._sections: list[type[Section]] = []
        self._fields: dict[str, _Field] = {}
        self._schema_problems: list[str] = []
        self._resolved: dict[str, object] = {}
        for section in sections:
            self._add_section(section)

    def load(self) -> Settings:
        merged = self._merge_until_schema_is_complete()
        problems = [*self._schema_problems, *merged.problems]
        values = {key: field.default for key, field in self._fields.items()}
        for key, raw in merged.values.items():
            known = self._fields.get(key)
            if known is None:
                if not self._ignore_unknown:
                    origin = merged.origins[key]
                    problems.append(f"unknown setting {key!r} from {origin}{did_you_mean(key, self._fields)}")
            elif known.is_registry:
                values[key] = raw
            else:
                try:
                    values[key] = _convert(raw, known.type, lenient=merged.lenient[key])
                except (msgspec.ValidationError, orjson.JSONDecodeError) as exc:
                    problems.append(f"{key}: {exc} (from {merged.origins[key]})")
        if problems:
            raise SettingsError(problems)
        for key, known in self._fields.items():
            if known.is_registry:
                values[key] = MappingProxyType(dict(values[key]))
        sections = self._build_sections(values, merged.origins)
        return Settings(values, origins=merged.origins, sections=sections)

    def _merge_until_schema_is_complete(self) -> _Merged:
        # Components named in component maps can register Sections that declare more
        # keys, including more component maps, so merge again until nothing new appears.
        while True:
            merged = self._merge()
            if not self._discover_sections(merged):
                return merged

    def _merge(self) -> _Merged:
        merged = _Merged()
        for key, known in self._fields.items():
            if known.is_registry and known.default:
                origin = f"the default of {known.section.__qualname__}"
                merged.values[key] = self._registry_entries(known, known.default, origin, False, merged.problems)
        for source in self._sources:
            for key, value in _flatten(source.values, self._fields):
                origin = source.describe(key)
                known = self._fields.get(key)
                if known is not None and known.is_registry:
                    entries = self._registry_entries(known, value, origin, source.lenient, merged.problems)
                    merged.values.setdefault(key, {}).update(entries)
                else:
                    merged.values[key] = value
                    merged.lenient[key] = source.lenient
                merged.origins[key] = origin
        return merged

    def _registry_entries(
        self, known: _Field, value: object, origin: str, lenient: bool, problems: list[str]
    ) -> dict[Any, Any]:
        key = known.section.key(known.name)
        if lenient and isinstance(value, str):
            try:
                value = orjson.loads(value)
            except orjson.JSONDecodeError as exc:
                problems.append(f"{key}: expected a JSON object, {exc} (from {origin})")
                return {}
        shape = "name to component" if known.is_named_components else "component to order"
        if not isinstance(value, Mapping):
            problems.append(f"{key}: expected a mapping of {shape}, got {value!r} (from {origin})")
            return {}
        entries = cast("Mapping[object, object]", value)
        if known.is_named_components:
            return self._named_entries(key, entries, origin, problems)
        return self._ordered_entries(key, entries, origin, problems)

    def _named_entries(
        self, key: str, value: Mapping[object, object], origin: str, problems: list[str]
    ) -> dict[str, object | None]:
        entries: dict[str, object | None] = {}
        for name, spec in value.items():
            if not isinstance(name, str) or not name:
                problems.append(f"{key}: names must be non-empty strings, got {name!r} (from {origin})")
                continue
            if spec is None or spec is False:
                entries[name] = None
                continue
            try:
                entries[name] = self._resolve(spec)
            except ComponentError as exc:
                problems.append(f"{key}: {exc} (from {origin})")
        return entries

    def _ordered_entries(
        self, key: str, value: Mapping[object, object], origin: str, problems: list[str]
    ) -> dict[object, int | None]:
        entries: dict[object, int | None] = {}
        for spec, order in value.items():
            if order is False:
                order = None
            if order is not None and (isinstance(order, bool) or not isinstance(order, int)):
                problems.append(f"{key}: the order of {spec!r} must be an integer, null, or false (from {origin})")
                continue
            try:
                component = self._resolve(spec)
            except ComponentError as exc:
                problems.append(f"{key}: {exc} (from {origin})")
                continue
            try:
                entries[component] = order
            except TypeError:
                problems.append(f"{key}: component {spec!r} is not hashable (from {origin})")
        return entries

    def _resolve(self, spec: object) -> object:
        if not isinstance(spec, str):
            return spec
        if spec not in self._resolved:
            self._resolved[spec] = resolve_component(spec)
        return self._resolved[spec]

    def _discover_sections(self, merged: _Merged) -> bool:
        discovered = False
        for key, known in list(self._fields.items()):
            if not known.is_registry:
                continue
            registry: Mapping[object, object] = merged.values.get(key, {})
            components = registry.values() if known.is_named_components else registry.keys()
            for component in [component for component in components if component is not None]:
                for section in _sections_of(component, {s.__name__: s for s in self._sections}):
                    discovered |= self._add_section(section)
        return discovered

    def _add_section(self, section: object) -> bool:
        if not (isinstance(section, type) and issubclass(section, Section)):
            self._schema_problems.append(f"{section!r} is not a settings Section")
            return False
        if section in self._sections:
            return False
        self._sections.append(section)
        # Read the defaults field by field: building the Section would run its
        # __post_init__ checks, which apply to the final values only.
        fields = structs.fields(section)
        missing = [info.name for info in fields if info.required]
        if missing:
            self._schema_problems.append(
                f"settings section {section.__qualname__} must give every setting a default; missing for {missing}"
            )
            return True
        for info in fields:
            key = section.key(info.name)
            existing = self._fields.get(key)
            if existing is not None:
                self._schema_problems.append(
                    f"setting {key!r} is declared by both {existing.section.__qualname__} and {section.__qualname__}"
                )
                continue
            default = info.default if info.default_factory is msgspec.NODEFAULT else info.default_factory()
            if info.type not in (ComponentMap, NamedComponents):
                try:
                    default = _convert(default, info.type, lenient=False)
                except msgspec.ValidationError as exc:
                    self._schema_problems.append(
                        f"settings section {section.__qualname__}: the default of {info.name!r} is invalid: {exc}"
                    )
            self._fields[key] = _Field(section, info.name, info.type, default)
        return True

    def _build_sections(self, values: Mapping[str, Any], origins: Mapping[str, str]) -> dict[type[Section], Section]:
        """Build every Section, reporting the problems their `__post_init__` checks find all at once."""
        built: dict[type[Section], Section] = {}
        problems: list[str] = []
        for section in self._sections:
            keys = {info.name: section.key(info.name) for info in structs.fields(section)}
            try:
                built[section] = section(**{name: values[key] for name, key in keys.items()})
            except (TypeError, ValueError) as exc:
                sources = sorted({origins[key] for key in keys.values() if key in origins})
                problems.append(f"{exc} (from {', '.join(sources)})" if sources else str(exc))
        if problems:
            raise SettingsError(problems)
        return built


def _flatten(values: Mapping[str, Any], known: Mapping[str, _Field], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield dotted keys, descending into nested tables that are not known settings."""
    for name, value in values.items():
        key = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(value, Mapping) and key not in known:
            yield from _flatten(value, known, key)  # pyright: ignore[reportUnknownArgumentType]
        else:
            yield key, value


def _sections_of(component: object, known_sections: Mapping[str, type]) -> list[object]:
    sections: list[object] = list(getattr(component, "settings_sections", ()))
    if isinstance(component, type):
        hints = init_type_hints(component, known_types=known_sections)
        sections += [hint for hint in hints.values() if isinstance(hint, type) and issubclass(hint, Section)]
    return sections


def _convert(value: object, target: Any, *, lenient: bool) -> Any:
    if lenient and isinstance(value, str) and _is_container(target):
        value = orjson.loads(value)
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
    return (origin or target) in _CONTAINERS
