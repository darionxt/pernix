"""Requests a Callback constructs taking the page they come from as their Referer, with http.inherit_referer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Response, Spider, spider_middleware
from pernix.testing import Answer, FakeFetcher, crawl

if TYPE_CHECKING:
    import re
    from collections.abc import AsyncIterator

    from pernix.middlewares import HandleResponse

SHOP = "https://shop.example"
FETCH = {"Sec-Fetch-Dest": "empty"}


class Pages(Spider):
    start_urls = (f"{SHOP}/",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        await self.fetch("/inline")
        yield Request("/books", callback=self.parse_books)
        yield Request("/api/page/1", headers=FETCH, callback=self.parse_api)
        yield Request("/elsewhere", headers={"Referer": ""}, callback=self.parse_books)
        yield Request("/given", headers={"Referer": f"{SHOP}/search"}, callback=self.parse_books)

    async def parse_books(self, response: Response) -> AsyncIterator[Any]:
        yield {"url": response.url}

    async def parse_api(self, response: Response) -> AsyncIterator[Any]:
        if response.url.endswith("/1"):
            yield Request("/api/page/2", headers=FETCH, callback=self.parse_api)


def _fake() -> FakeFetcher:
    routes: dict[str | re.Pattern[str], Answer | list[Answer]] = {f"{SHOP}/": "<p>shop</p>"}
    routes |= {f"{SHOP}{path}": "{}" for path in _PATHS}
    return FakeFetcher(routes)


_PATHS = ("/inline", "/books", "/api/page/1", "/api/page/2", "/elsewhere", "/given")


def _referers(fake: FakeFetcher) -> dict[str, str | None]:
    return {request.url.removeprefix(SHOP): request.headers.get("Referer") for request in fake.requests}


async def test_requests_from_callbacks_take_the_page_they_come_from() -> None:
    fake = _fake()

    await crawl(Pages, fetcher=fake, settings={"http.inherit_referer": True})

    assert _referers(fake) == {
        "/": None,
        "/inline": f"{SHOP}/",
        "/books": f"{SHOP}/",
        "/api/page/1": f"{SHOP}/",
        "/api/page/2": f"{SHOP}/",  # the page the API Response came from
        "/elsewhere": "",
        "/given": f"{SHOP}/search",
    }


async def test_without_the_setting_requests_carry_only_the_referer_they_are_given() -> None:
    fake = _fake()

    await crawl(Pages, fetcher=fake)

    assert _referers(fake) == {
        "/": None,
        "/inline": None,
        "/books": None,
        "/api/page/1": None,
        "/api/page/2": None,
        "/elsewhere": "",
        "/given": f"{SHOP}/search",
    }


class Quiet(Spider):
    start_urls = (f"{SHOP}/",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for _ in ():
            yield


@spider_middleware
async def follow_books(response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
    async for output in call_next(response):
        yield output
    if response.url == f"{SHOP}/":
        yield Request("/books")


@pytest.mark.parametrize("inherit", [True, False])
async def test_requests_a_spider_middleware_constructs_take_the_page_of_its_response(inherit: bool) -> None:
    fake = _fake()
    settings = {"http.inherit_referer": inherit, "spider_middlewares": {follow_books: 100}}

    await crawl(Quiet, fetcher=fake, settings=settings)

    assert _referers(fake)["/books"] == (f"{SHOP}/" if inherit else None)
