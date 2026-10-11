from __future__ import annotations

import inspect
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any, ClassVar

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from pernix.components import load_object
from pernix.errors import AddonError, ComponentError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.events import Event, EventBus, Subscriber
    from pernix.settings import Settings

__all__ = [
    "ADDON_API_VERSION",
    "ENTRY_POINT_GROUP",
    "Addon",
    "AddonAPI",
    "addon_settings",
    "discover_addons",
    "load_addons",
    "setup_addon",
]

ADDON_API_VERSION = "1.0"
"""The version of the Addon API this Pernix provides."""

ENTRY_POINT_GROUP = "pernix.addons"
"""The entry point group through which installed packages advertise Addons."""


class Addon:
    """Base class for Addons: a bundle of settings changes and registrations.

    Addons are enabled explicitly with the `addons` setting, by entry point name,
    import path, class, or object; an installed Addon is never loaded otherwise.
    Subclasses must set `api_version` to a version range such as `">=1,<2"`.

    Example:
        >>> class RedisFrontier(Addon):
        ...     api_version = ">=1,<2"
        ...
        ...     def update_settings(self, settings):
        ...         settings["extensions"] = {"pernix_redis.Monitor": 100}
    """

    api_version: ClassVar[str]

    def update_settings(self, settings: dict[str, Any]) -> None:
        """Add settings at the Addon layer, below the project file. Does nothing by default.

        Args:
            settings: This Addon's layer, initially empty; dotted keys or nested
                mappings, and component maps add or override entries.
        """

    def setup(self, api: AddonAPI) -> None:
        """Register subscriptions before the Run starts. Does nothing by default.

        It must not perform I/O; resources belong in an Extension's `open()`.

        Args:
            api: The registration API, valid only during this call.
        """


class AddonAPI:
    """What an Addon may do during `setup()`: read settings and register subscribers."""

    def __init__(self, *, addon: str, events: EventBus, settings: Settings) -> None:
        """Create the API for one Addon's setup.

        Args:
            addon: The Addon's name, for error messages.
            events: The Crawler's event bus.
            settings: The final settings of the Run.
        """
        self._addon = addon
        self._events = events
        self._settings = settings
        self._active = True

    @property
    def settings(self) -> Settings:
        """The final settings of the Run."""
        return self._settings

    def on(
        self,
        event_type: type[Event],
        subscriber: Subscriber,
        *,
        priority: int = 0,
        timeout: float | None = None,
    ) -> None:
        """Subscribe to an Event class; see `EventBus.on()`.

        Raises:
            AddonError: Called after `setup()` returned.
        """
        if not self._active:
            raise AddonError(f"Addon {self._addon} registered a subscriber after setup() returned")
        self._events.on(event_type, subscriber, priority=priority, timeout=timeout)

    def close(self) -> None:
        """End the registration window."""
        self._active = False


def discover_addons() -> dict[str, EntryPoint]:
    """List the Addons installed packages advertise, without loading them.

    Returns:
        Entry point name to entry point, for the `pernix.addons` group.
    """
    return {entry_point.name: entry_point for entry_point in entry_points(group=ENTRY_POINT_GROUP)}


def load_addons(specs: Iterable[object]) -> list[object]:
    """Load and check the enabled Addons, in order.

    Args:
        specs: Entry point names, import paths, Addon classes (instantiated without
            arguments), or Addon objects.

    Returns:
        The Addon objects.

    Raises:
        AddonError: An Addon cannot be found or loaded, or its `api_version` does not
            include `ADDON_API_VERSION`.
    """
    installed: dict[str, EntryPoint] | None = None
    addons: list[object] = []
    for spec in specs:
        if isinstance(spec, str):
            if installed is None:
                installed = discover_addons()
            target = _load_named(spec, installed)
        else:
            target = spec
        addon = target() if isinstance(target, type) else target
        _check_api_version(addon)
        addons.append(addon)
    return addons


def addon_settings(addon: object) -> dict[str, Any]:
    """Collect the settings an Addon contributes.

    Args:
        addon: A loaded Addon.

    Returns:
        The Addon's settings layer.
    """
    layer: dict[str, Any] = {}
    update = getattr(addon, "update_settings", None)
    if callable(update):
        update(layer)
    return layer


def setup_addon(addon: object, *, events: EventBus, settings: Settings) -> None:
    """Run an Addon's `setup()` with an API that only accepts registrations.

    Args:
        addon: A loaded Addon.
        events: The Crawler's event bus.
        settings: The final settings of the Run.

    Raises:
        AddonError: `setup()` is a coroutine function; it must not perform I/O.
    """
    setup = getattr(addon, "setup", None)
    if not callable(setup):
        return
    api = AddonAPI(addon=_name(addon), events=events, settings=settings)
    try:
        result = setup(api)
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise AddonError(f"Addon {_name(addon)}: setup() must be synchronous; put I/O in an Extension's open()")
    finally:
        api.close()


def _load_named(spec: str, installed: dict[str, EntryPoint]) -> object:
    if spec in installed:
        try:
            return installed[spec].load()
        except Exception as exc:
            raise AddonError(f"cannot load the installed Addon {spec!r}: {exc}") from exc
    try:
        return load_object(spec)
    except ComponentError as exc:
        names = ", ".join(sorted(installed)) or "none"
        raise AddonError(f"{spec!r} is neither an installed Addon (installed: {names}) nor importable: {exc}") from exc


def _check_api_version(addon: object) -> None:
    declared = getattr(addon, "api_version", None)
    if not isinstance(declared, str):
        raise AddonError(f"Addon {_name(addon)} does not declare an api_version range such as '>=1,<2'")
    try:
        supported = SpecifierSet(declared)
    except InvalidSpecifier as exc:
        raise AddonError(f"Addon {_name(addon)} declares an invalid api_version {declared!r}: {exc}") from exc
    if ADDON_API_VERSION not in supported:
        raise AddonError(
            f"Addon {_name(addon)} requires Addon API {declared}, but this Pernix provides {ADDON_API_VERSION}"
        )


def _name(addon: object) -> str:
    return type(addon).__qualname__
