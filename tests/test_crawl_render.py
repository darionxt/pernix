"""Runs that render some of their Requests in the browser."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Response, Spider
from pernix.testing import crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    for name in list(sys.modules):
        if name == "patchright" or name.startswith("patchright."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "patchright", None)  # starting it would fail

    report = await crawl(Plain, start=server.url("/"))

    assert report.items == [{"text": "plain"}]
    assert report.reason == "finished"
