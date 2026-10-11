from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

from pernix.engine import Engine
from pernix.feeds import JsonLinesFeed
from pernix.fetchers import HTTPFetcher
from pernix.memory_frontier import MemoryFrontier

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from pernix.spider import Spider

__all__ = ["Crawler"]


class Crawler:
    """Assembles the components for one Run of a Spider and runs it."""

    def __init__(
        self,
        spider: Spider,
        *,
        output: str | os.PathLike[str] | None = None,
        concurrency: int = 16,
    ) -> None:
        """Configure a Run.

        Args:
            spider: The Spider to run.
            output: A JSON-lines file for the Items; `None` discards them.
            concurrency: The maximum number of Requests processed at once.
        """
        self.spider = spider
        self._output = output
        self._concurrency = concurrency

    async def crawl(self) -> None:
        """Run the Spider to completion."""
        with ExitStack() as stack:
            item_sink = _discard if self._output is None else stack.enter_context(JsonLinesFeed(self._output)).write
            async with HTTPFetcher(max_clients=self._concurrency) as fetcher:
                engine = Engine(
                    self.spider,
                    frontier=MemoryFrontier(),
                    fetcher=fetcher,
                    item_sink=item_sink,
                    concurrency=self._concurrency,
                )
                await engine.run()


def _discard(_item: Mapping[str, Any]) -> None:
    pass
