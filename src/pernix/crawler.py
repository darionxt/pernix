from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from pernix.components import build_component
from pernix.engine import Engine
from pernix.feeds import JsonLinesFeed
from pernix.fetchers import HTTPFetcher
from pernix.memory_frontier import MemoryFrontier
from pernix.settings import CoreSettings, Section, Settings

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from pernix.spider import Spider

__all__ = ["Crawler"]


class Crawler:
    """Assembles the components for one Run of a Spider and runs it.

    Attributes:
        spider: The Spider being run.
        settings: The validated, immutable settings of this Run.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        settings: Settings,
        output: str | os.PathLike[str] | None = None,
    ) -> None:
        """Configure a Run.

        Args:
            spider: The Spider to run.
            settings: Settings created by `pernix.settings.load_settings()`.
            output: A JSON-lines file for the Items; `None` discards them.
        """
        self.spider = spider
        self.settings = settings
        self._output = output

    def build_component(self, spec: object) -> object:
        """Construct a component for this Run.

        Objects are used as is; classes are built through `from_crawler(crawler)` if
        they define it, and otherwise get their `__init__` parameters annotated with
        `Crawler`, `Settings`, or a registered Section injected.

        Args:
            spec: A class, an object, or an import path.

        Returns:
            The component.

        Raises:
            pernix.errors.ComponentError: The component cannot be loaded or built.
        """
        return build_component(spec, crawler=self, provide=self._provide)

    async def crawl(self) -> None:
        """Run the Spider to completion."""
        concurrency = self.settings.section(CoreSettings).concurrency
        with ExitStack() as stack:
            item_sink = _discard if self._output is None else stack.enter_context(JsonLinesFeed(self._output)).write
            async with HTTPFetcher(max_clients=concurrency) as fetcher:
                engine = Engine(
                    self.spider,
                    frontier=MemoryFrontier(),
                    fetcher=fetcher,
                    item_sink=item_sink,
                    concurrency=concurrency,
                )
                await engine.run()

    def _provide(self, service_type: type) -> object | None:
        # Match annotations exactly: isinstance() would fail on Any and on Protocols
        # that are not runtime-checkable, and would inject for loose types like object.
        if service_type in (Crawler, type(self)):
            return self
        if service_type is Settings:
            return self.settings
        if issubclass(service_type, Section):
            try:
                return self.settings.section(service_type)
            except KeyError:
                return None
        return None


def _discard(_item: Mapping[str, Any]) -> None:
    pass
