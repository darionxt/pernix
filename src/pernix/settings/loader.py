from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, UnionType
from typing import Annotated, Any, TypeAliasType, Union, cast, get_args, get_origin

import msgspec
import orjson
from msgspec import structs

from pernix.components import init_type_hints, resolve_component
from pernix.errors import ComponentError, SettingsError
from pernix.settings.core_settings import CoreSettings
from pernix.settings.secret import Secret
from pernix.settings.section import ComponentMap, Section
from pernix.settings.settings import Settings
from pernix.settings.sources import find_project_file, read_environment
from pernix.settings.suggestions import did_you_mean

__all__ = ["load_settings"]

_CONTAINERS: tuple[object, ...] = (dict, list, tuple, set, frozenset, Mapping, Sequence)


def load_settings(
    *,
    sections: Iterable[type[Section]] = (CoreSettings,),
    addon_layers: Sequence[Mapping[str, Any]] = (),
    project_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    spider_settings: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
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
    return _Loader(sources, sections).load()


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


@dataclass(slots=True)
class _Merged:
    values: dict[str, Any] = field(default_factory=dict[str, Any])
    origins: dict[str, str] = field(default_factory=dict[str, str])
    lenient: dict[str, bool] = field(default_factory=dict[str, bool])
    problems: list[str] = field(default_factory=list[str])


class _Loader:
    def __init__(self, sources: Sequence[_Source], sections: Iterable[type[Section]]) -> None:
        self._sources = sources
        self._sections: list[type[Section]] = []
        self._fields: dict[str, _Field] = {}
        self._schema_problems: list[str] = []
        self._resolved: dict[str, object] = {}
        for section in sections:
            self._add_section(section)

    def load(self) -> Settings:
        merged, discovery_problems = self._merge_until_schema_is_complete()
        problems = [*self._schema_problems, *merged.problems, *discovery_problems]
        values = {key: field.default for key, field in self._fields.items()}
        for key, raw in merged.values.items():
            known = self._fields.get(key)
            if known is None:
                origin = merged.origins[key]
                problems.append(f"unknown setting {key!r} from {origin}{did_you_mean(key, self._fields)}")
            elif known.is_component_map:
                values[key] = raw
            else:
                try:
                    values[key] = _convert(raw, known.type, lenient=merged.lenient[key])
                except (msgspec.ValidationError, orjson.JSONDecodeError) as exc:
                    problems.append(f"{key}: {exc} (from {merged.origins[key]})")
        if problems:
            raise SettingsError(problems)
        for key, known in self._fields.items():
            if known.is_component_map:
                values[key] = MappingProxyType(dict(values[key]))
        return Settings(values, origins=merged.origins, sections=self._build_sections(values))

    def _merge_until_schema_is_complete(self) -> tuple[_Merged, list[str]]:
        # Components named in component maps can register Sections that declare more
        # keys, including more component maps, so merge again until nothing new appears.
        while True:
            merged = self._merge()
            discovered, problems = self._discover_sections(merged)
            if not discovered:
                return merged, problems

    def _merge(self) -> _Merged:
        merged = _Merged()
        for key, known in self._fields.items():
            if known.is_component_map and known.default:
                origin = f"the default of {known.section.__qualname__}"
                merged.values[key] = self._component_entries(key, known.default, origin, False, merged.problems)
        for source in self._sources:
            for key, value in _flatten(source.values, self._fields):
                origin = source.describe(key)
                known = self._fields.get(key)
                if known is not None and known.is_component_map:
                    entries = self._component_entries(key, value, origin, source.lenient, merged.problems)
                    merged.values.setdefault(key, {}).update(entries)
                else:
                    merged.values[key] = value
                    merged.lenient[key] = source.lenient
                merged.origins[key] = origin
        return merged

    def _component_entries(
        self, key: str, value: object, origin: str, lenient: bool, problems: list[str]
    ) -> dict[object, int | None]:
        if lenient and isinstance(value, str):
            try:
                value = orjson.loads(value)
            except orjson.JSONDecodeError as exc:
                problems.append(f"{key}: expected a JSON object, {exc} (from {origin})")
                return {}
        if not isinstance(value, Mapping):
            problems.append(f"{key}: expected a mapping of component to order, got {value!r} (from {origin})")
            return {}
        entries: dict[object, int | None] = {}
        for spec, order in cast("Mapping[object, object]", value).items():
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

    def _discover_sections(self, merged: _Merged) -> tuple[bool, list[str]]:
        discovered = False
        problems: list[str] = []
        component_maps = [key for key, known in self._fields.items() if known.is_component_map]
        for key in component_maps:
            for component in merged.values.get(key, {}):
                try:
                    sections = _sections_of(component)
                except ComponentError as exc:
                    problems.append(f"{key}: {exc}")
                    continue
                for section in sections:
                    discovered |= self._add_section(section)
        return discovered, problems

    def _add_section(self, section: object) -> bool:
        if not (isinstance(section, type) and issubclass(section, Section)):
            self._schema_problems.append(f"{section!r} is not a settings Section")
            return False
        if section in self._sections:
            return False
        self._sections.append(section)
        try:
            defaults = section()
        except TypeError as exc:
            self._schema_problems.append(
                f"settings section {section.__qualname__} must give every setting a default: {exc}"
            )
            return True
        for info in structs.fields(section):
            key = section.key(info.name)
            existing = self._fields.get(key)
            if existing is not None:
                self._schema_problems.append(
                    f"setting {key!r} is declared by both {existing.section.__qualname__} and {section.__qualname__}"
                )
                continue
            default = getattr(defaults, info.name)
            if info.type is not ComponentMap:
                try:
                    default = _convert(default, info.type, lenient=False)
                except msgspec.ValidationError as exc:
                    self._schema_problems.append(
                        f"settings section {section.__qualname__}: the default of {info.name!r} is invalid: {exc}"
                    )
            self._fields[key] = _Field(section, info.name, info.type, default)
        return True

    def _build_sections(self, values: Mapping[str, Any]) -> dict[type[Section], Section]:
        built: dict[type[Section], Section] = {}
        for section in self._sections:
            fields = {info.name: values[section.key(info.name)] for info in structs.fields(section)}
            built[section] = section(**fields)
        return built


def _flatten(values: Mapping[str, Any], known: Mapping[str, _Field], prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield dotted keys, descending into nested tables that are not known settings."""
    for name, value in values.items():
        key = f"{prefix}.{name}" if prefix else str(name)
        if isinstance(value, Mapping) and key not in known:
            yield from _flatten(value, known, key)  # pyright: ignore[reportUnknownArgumentType]
        else:
            yield key, value


def _sections_of(component: object) -> list[object]:
    sections: list[object] = list(getattr(component, "settings_sections", ()))
    if isinstance(component, type):
        hints = init_type_hints(component)
        sections += [hint for hint in hints.values() if isinstance(hint, type) and issubclass(hint, Section)]
    return sections


def _convert(value: object, target: Any, *, lenient: bool) -> Any:
    if lenient and isinstance(value, str) and _is_container(target):
        value = orjson.loads(value)
    return msgspec.convert(value, target, strict=not lenient, dec_hook=_decode_custom)


def _decode_custom(target: type, value: object) -> object:
    if target is Secret:
        if isinstance(value, Secret):
            return value
        if isinstance(value, str):
            return Secret(value)
    raise TypeError(f"Expected `{target.__name__}`, got `{type(value).__name__}`")


def _is_container(target: Any) -> bool:
    if isinstance(target, TypeAliasType):
        return _is_container(target.__value__)
    origin = get_origin(target)
    if origin is Annotated:
        return _is_container(get_args(target)[0])
    if origin in (Union, UnionType):
        return any(_is_container(member) for member in get_args(target))
    return (origin or target) in _CONTAINERS
