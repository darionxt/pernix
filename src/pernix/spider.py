from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pernix.crawler import Crawler
from pernix.request import Request

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Sequence

    from pernix.request import CallbackResult
    from pernix.response import Response

__all__ = ["Spider"]


class Spider:
    """Base class for user crawling logic.

    Subclasses set `start_urls` or override `start()`, and implement `parse()` and any
    other Callbacks as async generators that yield Requests and Items.

    Attributes:
        name: Identifies the Spider in logs; defaults to the lower-cased class name.
        start_urls: URLs fetched with a GET when `start()` is not overridden.

    Example:
        >>> class Titles(Spider):
        ...     start_urls = ["https://example.com/"]
        ...
        ...     async def parse(self, response):
        ...         yield {"title": response.css("title::text").get()}
    """

    name: ClassVar[str] = ""
    start_urls: ClassVar[Sequence[str]] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__.lower()

    async def start(self) -> AsyncIterator[Request]:
        """Yield the initial Requests.

        Yields:
            One GET Request per URL in `start_urls`, unless overridden.
        """
        for url in self.start_urls:
            yield Request(url)

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
    async def crawl(cls, *, output: str | os.PathLike[str] | None = None, concurrency: int = 16) -> None:
        """Run this Spider inside an existing event loop.

        Args:
            output: A JSON-lines file for the Items; `None` discards them.
            concurrency: The maximum number of Requests processed at once.
        """
        await Crawler(cls(), output=output, concurrency=concurrency).crawl()

    @classmethod
    def run(cls, *, output: str | os.PathLike[str] | None = None, concurrency: int = 16) -> None:
        """Run this Spider to completion in a new event loop.

        Args:
            output: A JSON-lines file for the Items; `None` discards them.
            concurrency: The maximum number of Requests processed at once.
        """
        asyncio.run(cls.crawl(output=output, concurrency=concurrency))
