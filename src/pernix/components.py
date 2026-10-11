from __future__ import annotations

import importlib
import inspect
import typing
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pernix.errors import ComponentError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

__all__ = ["build_component", "init_type_hints", "load_object", "resolve_component"]


def load_object(path: str) -> object:
    """Import the object an import path points to.

    Args:
        path: `"package.module.Name"` or `"package.module:Name"`; the part after `:`
            may be a dotted attribute path.

    Returns:
        The imported object.

    Raises:
        ComponentError: The path is malformed, or the module or attribute is missing.
    """
    module_name, separator, attribute = path.partition(":")
    if not separator:
        module_name, _, attribute = path.rpartition(".")
    if not module_name or not attribute:
        raise ComponentError(f"{path!r} is not an import path like 'package.module.Name'")
    try:
        target: object = importlib.import_module(module_name)
    except ImportError as exc:
        raise ComponentError(f"cannot import module {module_name!r} for {path!r}: {exc}") from exc
    for part in attribute.split("."):
        try:
            target = getattr(target, part)
        except AttributeError as exc:
            raise ComponentError(f"cannot load {path!r}: {module_name!r} has no attribute {attribute!r}") from exc
    return target


def resolve_component(spec: object) -> object:
    """Turn a component specification into the component itself.

    Args:
        spec: A class, an object, or an import path.

    Returns:
        The imported object for an import path; `spec` itself otherwise.

    Raises:
        ComponentError: An import path cannot be loaded.
    """
    return load_object(spec) if isinstance(spec, str) else spec


def init_type_hints(component: type, *, known_types: Mapping[str, type] | None = None) -> dict[str, Any]:
    """Resolve the annotations of a class's `__init__`, parameter by parameter.

    A name the component's module does not define at runtime, for example one it imports
    only under `TYPE_CHECKING`, is looked up in `known_types`, so linters may keep such
    imports type-only; names the module does define always win. Annotations that still
    cannot be resolved are left out.

    Args:
        component: The class to inspect.
        known_types: Types to resolve by name when the module does not define them.

    Returns:
        Parameter name to resolved annotation.
    """
    function = component.__init__
    annotations: dict[str, Any] = dict(getattr(function, "__annotations__", {}))
    # The module's own names win; known types only fill in names it lacks at runtime.
    namespace: dict[str, Any] = {**(known_types or {}), **getattr(function, "__globals__", {})}
    hints: dict[str, Any] = {}
    for name, annotation in annotations.items():
        if name == "return":
            continue
        holder = SimpleNamespace(__annotations__={name: annotation})
        try:
            hints[name] = typing.get_type_hints(holder, globalns=namespace)[name]
        except NameError:
            continue
    return hints


def build_component(
    spec: object,
    *,
    crawler: object,
    provide: Callable[[type], object | None],
    known_types: Mapping[str, type] | None = None,
) -> object:
    """Construct a component from its specification.

    An object is returned as is. A class with a `from_crawler(crawler)` classmethod is
    built through it. Any other class is instantiated with its `__init__` parameters
    filled by `provide`, which maps an annotated type to a service.

    Args:
        spec: A class, an object, or an import path.
        crawler: Passed to `from_crawler`.
        provide: Returns the service for an annotated parameter type, or `None` if it
            has none.
        known_types: The service types by name, for annotations whose types the
            component's module imports only under `TYPE_CHECKING`.

    Returns:
        The component.

    Raises:
        ComponentError: The component cannot be loaded, or a required `__init__`
            parameter has no service to inject.
    """
    component = resolve_component(spec)
    if not isinstance(component, type):
        return component
    from_crawler = getattr(component, "from_crawler", None)
    if callable(from_crawler):
        return from_crawler(crawler)
    return component(**_injected_arguments(component, provide, known_types))


def _injected_arguments(
    component: type, provide: Callable[[type], object | None], known_types: Mapping[str, type] | None
) -> dict[str, object]:
    hints = init_type_hints(component, known_types=known_types)
    arguments: dict[str, object] = {}
    for name, parameter in inspect.signature(component).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        hint = hints.get(name)
        service = provide(hint) if isinstance(hint, type) else None
        if service is not None and parameter.kind is not parameter.POSITIONAL_ONLY:
            arguments[name] = service
        elif parameter.default is parameter.empty:
            unresolved = name not in hints and parameter.annotation is not parameter.empty
            reason = f" (its annotation {parameter.annotation!r} cannot be resolved at runtime)" if unresolved else ""
            raise ComponentError(
                f"cannot build {component.__qualname__}: no service can be injected for the required "
                f"parameter {name!r}{reason}. Annotate it with Crawler, Settings, EventBus, or a settings "
                "Section, give it a default, or define a from_crawler() classmethod."
            )
    return arguments
