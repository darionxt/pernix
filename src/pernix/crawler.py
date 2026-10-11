from __future__ import annotations

import asyncio
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from pernix.addons import addon_settings, load_addons, setup_addon
from pernix.component_lifecycle import ComponentLifecycle
from pernix.components import build_component
from pernix.engine import Engine
from pernix.events import EventBus, SpiderClosed, SpiderOpened
from pernix.feeds import JsonLinesFeed
from pernix.fetchers import HTTPFetcher
from pernix.memory_frontier import MemoryFrontier
from pernix.settings import CoreSettings, EventSettings, Section, Settings, load_settings

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping, Sequence

    from pernix.spider import Spider

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

        Addons register their subscribers, Extensions are built and opened in order,
        `SpiderOpened` is emitted, the Engine runs, `SpiderClosed` is emitted, and the
        Extensions are closed in reverse order.

        Raises:
            RuntimeError: This Crawler already ran.
        """
        if self._started:
            raise RuntimeError("a Crawler runs only once; create a new one for another Run")
        self._started = True
        for addon in self._addons:
            setup_addon(addon, events=self.events, settings=self.settings)
        async with ComponentLifecycle() as lifecycle:
            for spec in self.settings.components("extensions"):
                extension = self.build_component(spec)
                self.events.subscribe_methods(extension)
                await lifecycle.open(extension)
            await self._run()

    async def _run(self) -> None:
        concurrency = self.settings.section(CoreSettings).concurrency
        with ExitStack() as stack:
            item_sink = _discard if self._output is None else stack.enter_context(JsonLinesFeed(self._output)).write
            async with HTTPFetcher(max_clients=concurrency) as fetcher:
                engine = Engine(
                    self.spider,
                    frontier=MemoryFrontier(),
                    fetcher=fetcher,
                    item_sink=item_sink,
                    events=self.events,
                    concurrency=concurrency,
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
