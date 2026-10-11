from __future__ import annotations

import importlib
import inspect
import typing
from typing import TYPE_CHECKING, Any

from pernix.errors import ComponentError

if TYPE_CHECKING:
    from collections.abc import Callable

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


def init_type_hints(component: type) -> dict[str, Any]:
    """Return the resolved type annotations of a class's `__init__`.

    Args:
        component: The class to inspect.

    Returns:
        A mapping of parameter name to annotation.

    Raises:
        ComponentError: An annotation names a type that is not importable at runtime,
            for example one imported only under `TYPE_CHECKING`.
    """
    try:
        return typing.get_type_hints(component.__init__)
    except NameError as exc:
        raise ComponentError(
            f"cannot resolve the __init__ annotations of {component.__qualname__}: {exc}. "
            "Types used for injection must be importable at runtime."
        ) from exc


def build_component(spec: object, *, crawler: object, provide: Callable[[type], object | None]) -> object:
    """Construct a component from its specification.

    An object is returned as is. A class with a `from_crawler(crawler)` classmethod is
    built through it. Any other class is instantiated with its `__init__` parameters
    filled by `provide`, which maps an annotated type to a service.

    Args:
        spec: A class, an object, or an import path.
        crawler: Passed to `from_crawler`.
        provide: Returns the service for an annotated parameter type, or `None` if it
            has none.

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
    return component(**_injected_arguments(component, provide))


def _injected_arguments(component: type, provide: Callable[[type], object | None]) -> dict[str, object]:
    hints = init_type_hints(component)
    arguments: dict[str, object] = {}
    for name, parameter in inspect.signature(component).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        hint = hints.get(name)
        service = provide(hint) if isinstance(hint, type) else None
        if service is not None and parameter.kind is not parameter.POSITIONAL_ONLY:
            arguments[name] = service
        elif parameter.default is parameter.empty:
            raise ComponentError(
                f"cannot build {component.__qualname__}: no service can be injected for the required "
                f"parameter {name!r}. Annotate it with Crawler, Settings, or a settings Section, "
                "give it a default, or define a from_crawler() classmethod."
            )
    return arguments
