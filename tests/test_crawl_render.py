"""Runs that render some of their Requests in the browser."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Middleware, Request, Response, Spider
from pernix.browser import Click, Render, RenderResult
from pernix.testing import FakeFetcher, crawl, make_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix.middlewares import Download
    from pernix.testing import LoopbackServer

LISTING = """<html><body><ul id="books"></ul>
<script>
for (const id of [1, 2]) {
  document.getElementById("books").insertAdjacentHTML("beforeend", `<li><a href="/book/${id}">Book ${id}</a></li>`);
}
</script></body></html>"""


class Books(Spider):
    name = "books"

    def __init__(self, start: str) -> None:
        self.start_url = start

    async def start(self) -> AsyncIterator[Request]:
        yield Request(self.start_url, render=True)  # the links exist once the scripts ran

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for href in response.css("#books a::attr(href)").getall():
            yield Request(href, callback=self.parse_book, kwargs={"listed_by": response.fetcher})

    async def parse_book(self, response: Response, listed_by: str) -> AsyncIterator[Any]:
        yield {"title": response.css("h1::text").get(), "listed_by": listed_by, "fetcher": response.fetcher}


@pytest.mark.usefixtures("browser_engine")
async def test_a_run_mixes_rendered_and_http_requests(server: LoopbackServer) -> None:
    server.route("/", LISTING, headers=[("Content-Type", "text/html")])
    for book in (1, 2):
        server.route(f"/book/{book}", f"<h1>Book {book}</h1>", headers=[("Content-Type", "text/html")])

    report = await crawl(Books, start=server.url("/"))

    assert sorted(report.items, key=lambda item: item["title"]) == [
        {"title": "Book 1", "listed_by": "browser", "fetcher": "http"},
        {"title": "Book 2", "listed_by": "browser", "fetcher": "http"},
    ]
    assert report.stats["downloads.responses"] == 3


class Plain(Spider):
    name = "plain"

    def __init__(self, start: str) -> None:
        self.start_url = start

    async def start(self) -> AsyncIterator[Request]:
        yield Request(self.start_url)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"text": response.css("p::text").get()}


async def test_a_run_that_renders_nothing_never_starts_the_browser(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.route("/", "<p>plain</p>", headers=[("Content-Type", "text/html")])
    _without_patchright(monkeypatch)

    report = await crawl(Plain, start=server.url("/"))

    assert report.items == [{"text": "plain"}]
    assert report.reason == "finished"


def _without_patchright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make patchright impossible to import, so that starting a browser would fail."""
    for name in list(sys.modules):
        if name == "patchright" or name.startswith("patchright."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "patchright", None)


class Shelf(Spider):
    name = "shelf"

    async def start(self) -> AsyncIterator[Request]:
        yield Request("https://shop.example/", render=Render(wait_for="#books li", actions=[Click("#more")]))

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        assert response.browser is not None
        yield {"evaluated": list(response.browser.evaluated)}
        for href in response.css("#books a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)

    async def parse_book(self, response: Response) -> AsyncIterator[Any]:
        yield {"title": response.css("h1::text").get(), "fetcher": response.fetcher, "browser": response.browser}


async def test_a_spider_that_renders_some_requests_runs_on_canned_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_patchright(monkeypatch)
    listing = make_response(
        "https://shop.example/",
        "<ul id='books'><li><a href='/book/1'>Dune</a></li></ul>",
        browser=RenderResult(evaluated=[3]),
    )
    fake = FakeFetcher({"https://shop.example/": listing, "https://shop.example/book/1": "<h1>Dune</h1>"})

    report = await crawl(Shelf, fetcher=fake, browser=fake)

    assert report.items == [{"evaluated": [3]}, {"title": "Dune", "fetcher": "http", "browser": None}]
    assert [request.render for request in fake.requests] == [
        Render(wait_for="#books li", actions=(Click("#more"),)),
        False,
    ]
    assert fake.unmatched == []


async def test_rendered_responses_of_a_stand_in_carry_an_empty_render_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_patchright(monkeypatch)
    pages = FakeFetcher({"https://shop.example/book/1": "<h1>Dune</h1>"})
    rendered = FakeFetcher({"https://shop.example/": "<ul id='books'><li><a href='/book/1'>Dune</a></li></ul>"})

    report = await crawl(Shelf, fetcher=pages, browser=rendered)

    assert report.items[0] == {"evaluated": []}
    assert [request.url for request in rendered.requests] == ["https://shop.example/"]
    assert [request.url for request in pages.requests] == ["https://shop.example/book/1"]


class UpgradeBooks(Middleware):
    """Renders the Requests for books, which need their scripts to run."""

    async def __call__(self, request: Request, call_next: Download) -> Response:
        if "/book/" in request.url and not request.render:
            request = request.replace(render=Render(wait_for="h1"))
        return await call_next(request)


async def test_a_middleware_upgrading_a_request_to_render_sends_it_to_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _without_patchright(monkeypatch)
    pages = FakeFetcher({"https://shop.example/": "<a href='/book/1'>Dune</a>"})
    browser = FakeFetcher({"https://shop.example/book/1": "<h1>Dune</h1>"})

    report = await crawl(Plainly, fetcher=pages, browser=browser, settings={"middlewares": {UpgradeBooks: 100}})

    assert report.items == [{"title": "Dune", "fetcher": "browser", "rendered": True}]
    assert [request.render for request in browser.requests] == [Render(wait_for="h1")]
    assert [request.url for request in pages.requests] == ["https://shop.example/"]


class Plainly(Spider):
    name = "plainly"

    async def start(self) -> AsyncIterator[Request]:
        yield Request("https://shop.example/")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for href in response.css("a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)

    async def parse_book(self, response: Response) -> AsyncIterator[Any]:
        yield {
            "title": response.css("h1::text").get(),
            "fetcher": response.fetcher,
            "rendered": response.browser is not None,
        }
