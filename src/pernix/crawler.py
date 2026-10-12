from __future__ import annotations

import asyncio
import functools
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, cast

from pernix.addons import addon_settings, load_addons, setup_addon
from pernix.component_lifecycle import ComponentLifecycle
from pernix.components import build_component
from pernix.engine import Engine, run_callbacks
from pernix.errors import ComponentError
from pernix.events import EventBus, SpiderClosed, SpiderOpened
from pernix.feeds import JsonLinesFeed
from pernix.fetchers import FetcherDispatch
from pernix.fingerprint import request_fingerprint
from pernix.memory_frontier import MemoryFrontier
from pernix.middleware import build_download_chain
from pernix.settings import (
    CoreSettings,
    EventSettings,
    FingerprintSettings,
    FrontierSettings,
    Section,
    Settings,
    load_settings,
)
from pernix.spider_middleware import build_spider_chain

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Mapping, Sequence

    from pernix.frontier import Frontier
    from pernix.middleware import Download
    from pernix.request import Request
    from pernix.spider import Spider
    from pernix.spider_middleware import HandleResponse

__all__ = ["Crawler"]


class Crawler:
    """Assembles the components for one Run of a Spider and runs it.

    Attributes:
        spider: The Spider being run.
        settings: The validated, immutable settings of this Run.
        events: The event bus of this Run.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        settings: Settings,
        output: str | os.PathLike[str] | None = None,
        addons: Sequence[object] = (),
    ) -> None:
        """Configure a Run. `from_spider()` also loads the settings and Addons.

        Args:
            spider: The Spider to run.
            settings: Settings created by `pernix.settings.load_settings()`.
            output: A JSON-lines file for the Items; `None` discards them.
            addons: Loaded Addons whose `setup()` runs before the Run starts.
        """
        self.spider = spider
        self.settings = settings
        self.events = EventBus(timeout=settings.section(EventSettings).timeout)
        self._output = output
        self._addons = tuple(addons)
        self._started = False

    @classmethod
    def from_spider(
        cls,
        spider_type: type[Spider],
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
    ) -> Crawler:
        """Load the settings, including enabled Addons, and create a Crawler.

        The `addons` setting is read first, ignoring keys that only the Addons'
        components declare; the settings are then loaded again with each Addon's
        layer.

        Args:
            spider_type: The Spider class; a new object is created for the Run.
            settings: Run arguments, the highest settings layer.
            output: A JSON-lines file for the Items; `None` discards them.

        Returns:
            The Crawler, not yet started.

        Raises:
            pernix.errors.SettingsError: The settings are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
        """
        enabled = load_settings(
            sections=(CoreSettings,), spider_settings=spider_type.settings, overrides=settings, ignore_unknown=True
        )
        addons = load_addons(enabled.section(CoreSettings).addons)
        final = load_settings(
            addon_layers=[addon_settings(addon) for addon in addons],
            spider_settings=spider_type.settings,
            overrides=settings,
        )
        return cls(spider_type(), settings=final, output=output, addons=addons)

    def build_component(self, spec: object) -> object:
        """Construct a component for this Run.

        Objects are used as is; classes are built through `from_crawler(crawler)` if
        they define it, and otherwise get their `__init__` parameters annotated with
        exactly `Crawler`, `Settings`, `EventBus`, or a registered Section injected.

        Args:
            spec: A class, an object, or an import path.

        Returns:
            The component.

        Raises:
            pernix.errors.ComponentError: The component cannot be loaded or built.
        """
        return build_component(spec, crawler=self, provide=self._provide, known_types=self._service_types())

    async def crawl(self) -> None:
        """Run the Spider to completion.

        Addons register their subscribers. Then every component is built: Extensions,
        Fetchers, download Middleware, spider Middleware, and the Fingerprint function,
        and the chains are checked, before anything is opened. The components are
        subscribed and opened in that order, `SpiderOpened` is emitted, the Engine runs
        with an in-memory Frontier, `SpiderClosed` is emitted, and the components are
        closed in reverse order.

        Raises:
            RuntimeError: This Crawler already ran.
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme.
        """
        if self._started:
            raise RuntimeError("a Crawler runs only once; create a new one for another Run")
        self._started = True
        for addon in self._addons:
            setup_addon(addon, events=self.events, settings=self.settings)
        extensions = self._build_all("extensions")
        fetchers = self._build_named("fetchers")
        middlewares = self._build_all("middlewares")
        spider_middlewares = self._build_all("spider_middlewares")
        download = build_download_chain(middlewares, FetcherDispatch(fetchers))
        handle = build_spider_chain(spider_middlewares, run_callbacks(self.spider))
        frontier = MemoryFrontier(fingerprint=self._fingerprint_function())
        async with ComponentLifecycle() as lifecycle:
            for component in (*extensions, *fetchers.values(), *middlewares, *spider_middlewares):
                self.events.subscribe_methods(component)
                await lifecycle.open(component)
            try:
                await self._run(frontier, download, handle)
            finally:
                await frontier.close()

    def _build_all(self, key: str) -> list[object]:
        """Build the enabled components of a `{component: order}` setting, in order."""
        return [self.build_component(spec) for spec in self.settings.components(key)]

    def _build_named(self, key: str) -> dict[str, object]:
        """Build the enabled components of a `{name: component}` setting."""
        return {name: self.build_component(spec) for name, spec in self.settings.named_components(key).items()}

    def _fingerprint_function(self) -> Callable[[Request], str]:
        """The `fingerprint.function` setting, or the default Fingerprint with its options."""
        options = self.settings.section(FingerprintSettings)
        if options.function is None:
            return functools.partial(request_fingerprint, headers=options.headers, sort_query=options.sort_query)
        function = self.build_component(options.function)
        if not callable(function):
            raise ComponentError(f"fingerprint.function must be callable as function(request) -> str, got {function!r}")
        return cast("Callable[[Request], str]", function)

    async def _run(self, frontier: Frontier, download: Download, handle: HandleResponse) -> None:
        concurrency = self.settings.section(CoreSettings).concurrency
        with ExitStack() as stack:
            item_sink = _discard if self._output is None else stack.enter_context(JsonLinesFeed(self._output)).write
            engine = Engine(
                self.spider,
                frontier=frontier,
                download=download,
                handle=handle,
                item_sink=item_sink,
                events=self.events,
                concurrency=concurrency,
                lease_ttl=self.settings.section(FrontierSettings).lease_ttl,
            )
            reason = "failed"
            try:
                await self.events.emit(SpiderOpened(self.spider))
                await engine.run()
                reason = "finished"
            except asyncio.CancelledError:
                reason = "cancelled"
                raise
            finally:
                await self.events.emit(SpiderClosed(self.spider, reason))

    def _service_types(self) -> dict[str, type]:
        sections = {section.__name__: section for section in self.settings.sections()}
        return {**sections, "Crawler": Crawler, "Settings": Settings, "EventBus": EventBus}

    def _provide(self, service_type: type) -> object | None:
        # Match annotations exactly: isinstance() would fail on Any and on Protocols
        # that are not runtime-checkable, and would inject for loose types like object.
        if service_type in (Crawler, type(self)):
            return self
        if service_type is Settings:
            return self.settings
        if service_type is EventBus:
            return self.events
        if issubclass(service_type, Section):
            try:
                return self.settings.section(service_type)
            except KeyError:
                return None
        return None


def _discard(_item: Mapping[str, Any]) -> None:
    pass
