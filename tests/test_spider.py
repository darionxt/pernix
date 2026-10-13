from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Headers, Request, Response, Spider
from pernix.crawler import Crawler
from pernix.settings import load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def test_spider_name_defaults_to_class_name() -> None:
    class ProductListing(Spider):
        pass

    assert ProductListing.name == "productlisting"


async def test_start_yields_a_get_per_start_url() -> None:
    class Listing(Spider):
        start_urls = ("https://example.com/a", "https://example.com/b")

    requests = [request async for request in Listing().start()]

    assert [(r.method, r.url, r.callback) for r in requests] == [
        ("GET", "https://example.com/a", None),
        ("GET", "https://example.com/b", None),
    ]


def test_default_parse_must_be_implemented() -> None:
    response = Response(
        url="https://example.com/", status=200, headers=Headers(), body=b"", request=Request("https://example.com/")
    )

    with pytest.raises(NotImplementedError, match=r"Spider\.parse\(\) is not implemented"):
        Spider().parse(response)


def test_generator_and_coroutine_overrides_satisfy_the_parse_contract() -> None:
    # Type-checked: both override styles must be accepted without suppressions.
    class Generating(Spider):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"url": response.url}

    class Returning(Spider):
        async def parse(self, response: Response) -> list[dict[str, str]]:
            return [{"url": response.url}]

    assert Generating.parse is not Spider.parse
    assert Returning.parse is not Spider.parse


def test_the_crawler_is_set_when_a_crawler_is_created() -> None:
    class Books(Spider):
        pass

    spider = Books()
    with pytest.raises(RuntimeError, match="Books has no Crawler yet"):
        _ = spider.crawler
    with pytest.raises(RuntimeError, match="no Crawler yet"):
        _ = spider.stats

    crawler = Crawler(spider, settings=load_settings())

    assert spider.crawler is crawler
    assert spider.stats is crawler.stats
    assert spider.logger is logging.getLogger("pernix.spiders.books")


def test_a_spider_object_belongs_to_one_crawler() -> None:
    class Books(Spider):
        pass

    spider = Books()
    Crawler(spider, settings=load_settings())

    with pytest.raises(RuntimeError, match="already belongs to another Crawler"):
        Crawler(spider, settings=load_settings())
