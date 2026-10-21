"""A Spider that tests run in processes of their own, killed and resumed, against the pages of a `LoopbackServer`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pernix import Request, Spider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix import Response

PAGES = 8
"""The pages the index links to, `/page/1` to `/page/8`."""


class Catalog(Spider):
    """Follows the links of `/index` to each page, and yields its URL."""

    settings = {"concurrency": 4, "stats.interval": 0, "log.level": "WARNING"}  # noqa: RUF012

    def __init__(self, base: str) -> None:
        self.base = base

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{self.base}/index")

    async def parse(self, response: Response) -> AsyncIterator[Request]:
        for href in response.css("a::attr(href)").getall():
            yield Request(href, callback=self.parse_page)

    async def parse_page(self, response: Response) -> AsyncIterator[dict[str, Any]]:
        yield {"page": response.url}


INDEX = "".join(f'<a href="/page/{n}">{n}</a>' for n in range(1, PAGES + 1))
