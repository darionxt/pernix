from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from pernix.crawler import Crawler
from pernix.request import Request
from pernix.runner import run_spider
from pernix.spider_arguments import check_argument_names

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Mapping, Sequence

    from pernix.items import ItemStream
    from pernix.request import CallbackResult
    from pernix.response import Response
    from pernix.run_result import RunResult
    from pernix.stats import Stats

__all__ = ["Spider"]


class Spider:
    """Base class for user crawling logic.

    Subclasses set `start_urls` or override `start()`, and implement `parse()` and any
    other Callbacks as async generators that yield Requests and Items.

    A Spider takes arguments through the keyword parameters of its `__init__`. Each
    Run creates a new Spider object with them, converting strings, such as those from
    a command line, to the annotated types; `__init__` may raise `ValueError` to
    reject them. `settings` and `output` cannot name an argument.

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
        check_argument_names(cls)

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
    def run(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> RunResult:
        """Run this Spider to completion in a new event loop, as the main program.

        SIGINT or SIGTERM drains the Run, and a second one force-stops it. When the
        Run does not complete, the process exits: with 1 when it failed, with 2 for a
        configuration error, such as invalid settings or Spider arguments, and with
        130 when a signal stopped it. Use `crawl()` to handle errors in code.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            **args: The Spider arguments.

        Returns:
            How the Run ended, when it finished or code stopped it.

        Raises:
            SystemExit: The Run failed, was misconfigured, or was stopped by a signal.

        Example:
            >>> if __name__ == "__main__":
            ...     Books.run(output="books.jsonl", settings={"concurrency": 32})
        """
        return run_spider(cls, args=args, settings=settings, output=output)

    @classmethod
    async def crawl(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> RunResult:
        """Run this Spider inside an existing event loop.

        Signals are left to the application; cancelling the task stops the Run like
        a Force Stop, and raises `CancelledError`. For more control, such as calling
        `stop()`, create a `pernix.crawler.Crawler` with `Crawler.from_spider()`.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            **args: The Spider arguments.

        Returns:
            How the Run ended.

        Raises:
            pernix.errors.SettingsError: The settings are invalid; nothing has been
                fetched or written yet.
            pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
        """
        return await Crawler.from_spider(cls, args=args, settings=settings, output=output).crawl()

    @classmethod
    def stream(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> ItemStream:
        """Run this Spider inside an existing event loop, streaming the Items that pass every Pipeline.

        The Run starts with the first iteration. The stream holds one Item at a time,
        so a slow consumer slows the Run down. Leaving early force-stops the Run; an
        `async with` block waits until it has stopped. If the Run fails, the error is
        raised after the Items already received.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting.
            **args: The Spider arguments.

        Returns:
            The Items, as an async iterator and an async context manager.

        Raises:
            pernix.errors.SettingsError: The settings are invalid.
            pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.

        Example:
            >>> async with Books.stream(keyword="dune") as items:
            ...     async for item in items:
            ...         print(item["title"])
        """
        return Crawler.from_spider(cls, args=args, settings=settings, output=output).stream()
