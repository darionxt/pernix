"""Running Spiders and Callbacks in tests, without a Platform or a command line."""

from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pernix.callbacks import iterate_and_close, run_callbacks
from pernix.crawler import Crawler

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.response import Response
    from pernix.spider import Spider
    from pernix.stats import StatValue

__all__ = ["CrawlReport", "crawl", "run_callback"]

_TEST_SETTINGS: Mapping[str, Any] = MappingProxyType(
    {
        "stats.interval": 0,
        "retry.delay_scale": 0,
        "domain.delay": 0,
        "domain.rate": None,
        "domain.cooldown_max": 0,
    }
)
"""Settings that would only make a test wait: periodic stats, retry delays, and per-domain pacing."""


@dataclass(frozen=True, slots=True)
class CrawlReport:
    """What a Run of `crawl()` produced.

    Attributes:
        items: The Items that passed every Pipeline, in the order they did.
        stats: The final stats of the Run, such as `items.scraped`.
        reason: How the Run ended, such as `"finished"`.
        run_id: The id of the Run.
    """

    items: list[Any]
    stats: Mapping[str, StatValue]
    reason: str
    run_id: str


async def crawl(
    spider_type: type[Spider],
    *,
    fetcher: object | None = None,
    settings: Mapping[str, Any] | None = None,
    **args: Any,
) -> CrawlReport:
    """Run a Spider to completion for a test, and report its Items and stats.

    The Run takes `settings` over settings that keep a test from waiting: no periodic
    stats, no retry delays, and no per-domain delay, rate, or cooldown. The project
    file, the environment, and the Spider's own settings apply below them, as for any
    Run.

    Args:
        spider_type: The Spider class; a new object is created for the Run.
        fetcher: A Fetcher, such as a `FakeFetcher`, registered as `http` in place of
            the HTTP Fetcher.
        settings: Settings for this Run, by dotted key.
        **args: The Spider arguments.

    Returns:
        The Items and the stats of the Run.

    Raises:
        Exception: The Run failed, or its settings or arguments are invalid.

    Example:
        >>> report = await crawl(Books, fetcher=FakeFetcher({"https://shop.example/": "<h1>Dune</h1>"}))
        >>> report.items
        [{'title': 'Dune'}]
    """
    overrides: dict[str, Any] = {**_TEST_SETTINGS, **(settings or {})}
    if fetcher is not None:
        overrides["fetchers"] = {**overrides.get("fetchers", {}), "http": fetcher}
    crawler = Crawler.from_spider(spider_type, args=args, settings=overrides)
    async with crawler.stream_items() as stream:
        items = [item async for item in stream]
    stats = crawler.stats.snapshot()
    return CrawlReport(
        items=items, stats=MappingProxyType(stats), reason=str(stats["run.reason"]), run_id=crawler.run_id
    )


async def run_callback(spider: Spider, response: Response) -> list[Any]:
    """Run the Callback of a Response's Request as a Run would, and return what it produced.

    The Callback is the Request's, or `parse`, called with the Request's `kwargs`, and
    the Requests it constructs resolve relative URLs against the Response. Spider
    Middleware do not run, and Inline Fetches need a running Run: test Callbacks that
    fetch inline with `crawl()`.

    Args:
        spider: The Spider whose Callback to run.
        response: The Response, such as one `make_response()` built.

    Returns:
        The Requests and Items it produced, in order, without the `None`s a Run
        ignores.

    Example:
        >>> outputs = await run_callback(Books(), make_response(url, "<a href='/book/1'>Dune</a>"))
        >>> [output.url for output in outputs]
        ['https://shop.example/book/1']
    """
    async with aclosing(iterate_and_close(run_callbacks(spider)(response))) as outputs:
        return [output async for output in outputs if output is not None]
