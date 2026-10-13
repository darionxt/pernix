from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from pernix.crawler import Crawler
from pernix.request import Request

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Mapping, Sequence

    from pernix.request import CallbackResult
    from pernix.response import Response
    from pernix.stats import Stats

__all__ = ["Spider"]


class Spider:
    """Base class for user crawling logic.

    Subclasses set `start_urls` or override `start()`, and implement `parse()` and any
    other Callbacks as async generators that yield Requests and Items.

    Attributes:
        name: Identifies the Spider in logs; defaults to the lower-cased class name.
        start_urls: URLs fetched with a GET when `start()` is not overridden.
        allowed_domains: Domains, such as `"example.com"`, whose hosts and subdomains
            Requests may go to; Requests elsewhere are dropped as offsite. Empty allows
            every host.
        settings: Settings for this Spider, by dotted key; they override the project
            file and the environment, and are overridden by run arguments.
        crawler: The Crawler running this Spider.
        stats: The stats of the Run.
        logger: The logger `pernix.spiders.<name>`.

    Example:
        >>> class Titles(Spider):
        ...     start_urls = ["https://example.com/"]
        ...
        ...     async def parse(self, response):
        ...         yield {"title": response.css("title::text").get()}
    """

    name: ClassVar[str] = ""
    start_urls: ClassVar[Sequence[str]] = ()
    allowed_domains: ClassVar[Sequence[str]] = ()
    settings: ClassVar[Mapping[str, Any]] = MappingProxyType({})
    _crawler: Crawler | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__.lower()

    @property
    def crawler(self) -> Crawler:
        """The Crawler running this Spider; the Crawler sets it when it is created.

        Raises:
            RuntimeError: No Crawler was created for this Spider object yet.
        """
        if self._crawler is None:
            raise RuntimeError(f"{type(self).__name__} has no Crawler yet; one is set when a Crawler is created for it")
        return self._crawler

    @crawler.setter
    def crawler(self, crawler: Crawler) -> None:
        if self._crawler is not None and self._crawler is not crawler:
            raise RuntimeError(
                f"this {type(self).__name__} object already belongs to another Crawler; create one Spider object per Run"
            )
        self._crawler = crawler

    @property
    def stats(self) -> Stats:
        """The stats of the Run, as `self.crawler.stats`; add your own under your own prefix.

        Raises:
            RuntimeError: No Crawler was created for this Spider object yet.
        """
        return self.crawler.stats

    @property
    def logger(self) -> logging.Logger:
        """The logger `pernix.spiders.<name>`; its records carry the ids of the Run and the Request."""
        return logging.getLogger(f"pernix.spiders.{self.name}")

    async def start(self) -> AsyncIterator[Request]:
        """Yield the initial Requests.

        Yields:
            One GET Request per URL in `start_urls`, unless overridden.
        """
        for url in self.start_urls:
            yield Request(url)

    async def on_idle(self) -> AsyncIterator[Request]:
        """Yield more Requests once the Frontier is idle; the Run ends when none are added.

        It runs whenever nothing is ready, delayed, or being processed, after the
        `SpiderIdle` Event. The Run ends once it adds no new Request to the Frontier,
        including when everything it yields is a duplicate or vetoed. As in `start()`,
        URLs must be absolute.

        Yields:
            Nothing, unless overridden.
        """
        return
        yield  # makes this an async generator

    def parse(self, response: Response) -> CallbackResult:
        """Handle a Response whose Request names no Callback.

        Override it as an async generator that yields Requests and Items, or as a
        coroutine that returns one output or a list of them.

        Args:
            response: The Response to parse.

        Returns:
            The outputs, as an async iterator or an awaitable.

        Raises:
            NotImplementedError: The subclass did not implement it.
        """
        raise NotImplementedError(f"{type(self).__name__}.parse() is not implemented")

    @classmethod
    async def crawl(
        cls,
        *,
        output: str | os.PathLike[str] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        """Run this Spider inside an existing event loop.

        Args:
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.

        Raises:
            pernix.errors.SettingsError: The settings are invalid; nothing has been
                fetched or written yet.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
        """
        await Crawler.from_spider(cls, settings=settings, output=output).crawl()

    @classmethod
    def run(
        cls,
        *,
        output: str | os.PathLike[str] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> None:
        """Run this Spider to completion in a new event loop.

        Args:
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.

        Raises:
            pernix.errors.SettingsError: The settings are invalid; nothing has been
                fetched or written yet.
        """
        asyncio.run(cls.crawl(output=output, settings=settings))
