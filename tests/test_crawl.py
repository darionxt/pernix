"""End-to-end crawls over HTTP against the loopback server."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Request, Response, Spider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from loopback import LoopbackServer

PAGES = 3
FIRST_PAGE = "/catalogue/page-1.html"
BOOKS_PER_PAGE = 3


def _list_page(server: LoopbackServer, page: int) -> str:
    books = range((page - 1) * BOOKS_PER_PAGE + 1, page * BOOKS_PER_PAGE + 1)
    articles = "".join(
        f'<article><h3><a href="{server.url(f"/book/{n}.html")}">Book {n}</a></h3></article>' for n in books
    )
    first = f'<a class="first" href="{server.url(FIRST_PAGE)}">first</a>'
    after = server.url(f"/catalogue/page-{page + 1}.html")
    following = f'<li class="next"><a href="{after}">next</a></li>' if page < PAGES else ""
    return f"<html><body>{articles}{first}<ul>{following}</ul></body></html>"


def _book_page(n: int) -> str:
    return f'<html><body><h1>Book {n}</h1><p class="price_color">£{n}.00</p></body></html>'


@pytest.fixture
def bookstore(server: LoopbackServer) -> LoopbackServer:
    """A small paginated catalogue: list pages link to detail pages and to each other."""
    html = [("Content-Type", "text/html; charset=utf-8")]
    for page in range(1, PAGES + 1):
        server.route(f"/catalogue/page-{page}.html", _list_page(server, page), headers=html)
    for n in range(1, PAGES * BOOKS_PER_PAGE + 1):
        server.route(f"/book/{n}.html", _book_page(n), headers=html)
    return server


class Books(Spider):
    """The example from docs/design.md §4.1; the fixture pages use absolute links."""

    name = "books"

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for href in response.css("article h3 a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)
        if next_url := response.css("li.next a::attr(href)").get():
            yield Request(next_url)

    async def parse_book(self, response: Response) -> AsyncIterator[Any]:
        yield {
            "title": response.css("h1::text").get(),
            "price": response.css(".price_color::text").get(),
        }


def _read_items(path: Path) -> list[dict[str, Any]]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


EXPECTED_BOOKS = [{"title": f"Book {n}", "price": f"£{n}.00"} for n in range(1, PAGES * BOOKS_PER_PAGE + 1)]


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item["title"])


def test_run_crawls_list_and_detail_pages(bookstore: LoopbackServer, tmp_path: Path) -> None:
    class LocalBooks(Books):
        start_urls = (bookstore.url(FIRST_PAGE),)

    output = tmp_path / "books.jsonl"
    LocalBooks.run(output=output)

    assert _sorted(_read_items(output)) == _sorted(EXPECTED_BOOKS)
    fetched = [request.path for request in bookstore.received]
    assert len(fetched) == len(set(fetched)), "every page is fetched exactly once"
    assert len(fetched) == PAGES + PAGES * BOOKS_PER_PAGE


async def test_crawl_runs_inside_an_event_loop(bookstore: LoopbackServer, tmp_path: Path) -> None:
    class LocalBooks(Books):
        start_urls = (bookstore.url(FIRST_PAGE),)

    output = tmp_path / "books.jsonl"
    async with asyncio.timeout(10):
        await LocalBooks.crawl(output=output, concurrency=2)

    assert _sorted(_read_items(output)) == _sorted(EXPECTED_BOOKS)


async def test_callback_exception_is_logged_and_run_continues(
    bookstore: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Flaky(Books):
        start_urls = (bookstore.url(FIRST_PAGE),)

        async def parse_book(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/book/5.html"):
                raise ValueError("cannot parse book 5")
            async for item in super().parse_book(response):
                yield item

    output = tmp_path / "books.jsonl"
    async with asyncio.timeout(10):
        await Flaky.crawl(output=output)

    titles = {item["title"] for item in _read_items(output)}
    assert titles == {f"Book {n}" for n in range(1, PAGES * BOOKS_PER_PAGE + 1)} - {"Book 5"}
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "parse_book" in record.getMessage()
    assert "/book/5.html" in record.getMessage()


async def test_fetch_error_is_logged_and_run_continues(
    server: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    server.route("/ok", "ok")

    class Mixed(Spider):
        start_urls = (f"http://127.0.0.1:{closed_port}/", server.url("/ok"))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"body": response.text}

    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        await Mixed.crawl(output=output)

    assert _read_items(output) == [{"body": "ok"}]
    assert any(f":{closed_port}/" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


async def test_post_request_sends_body_and_headers(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/search", '{"hits": 2}', method="POST")

    class Search(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(
                server.url("/search"),
                method="post",
                body=b"q=books",
                headers=[("Content-Type", "application/x-www-form-urlencoded"), ("X-Trace", "1")],
                meta={"query": "books"},
            )

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"query": response.meta["query"], "status": response.status}

    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        await Search.crawl(output=output)

    assert _read_items(output) == [{"query": "books", "status": 200}]
    [received] = server.received
    assert received.method == "POST"
    assert received.body == b"q=books"
    assert received.header("X-Trace") == "1"


async def test_coroutine_callbacks_may_return_a_list(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "<p>a</p><p>b</p>")

    class Paragraphs(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> list[dict[str, str | None]]:  # type: ignore[override]
            return [{"text": text} for text in response.css("p::text").getall()]

    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        await Paragraphs.crawl(output=output)

    assert _read_items(output) == [{"text": "a"}, {"text": "b"}]


async def test_unsupported_outputs_are_logged(server: LoopbackServer, caplog: pytest.LogCaptureFixture) -> None:
    server.route("/", "ok")

    class Odd(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield 42

    async with asyncio.timeout(10):
        await Odd.crawl()

    assert any("unsupported output 42" in r.getMessage() for r in caplog.records)
