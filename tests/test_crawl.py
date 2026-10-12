"""End-to-end crawls over HTTP against the loopback server."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Request, Response, Spider, events
from pernix.errors import ComponentError, SettingsError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from loopback import LoopbackServer

PAGES = 3
FIRST_PAGE = "/catalogue/page-1.html"
BOOKS_PER_PAGE = 3


def _list_page(page: int, *, base: str | None = None) -> str:
    """A list page with links relative to /catalogue/, as written on real sites."""
    books = range((page - 1) * BOOKS_PER_PAGE + 1, page * BOOKS_PER_PAGE + 1)
    articles = "".join(f'<article><h3><a href="../book/{n}.html">Book {n}</a></h3></article>' for n in books)
    first = f'<a class="first" href="{FIRST_PAGE}">first</a>'
    following = f'<li class="next"><a href="page-{page + 1}.html">next</a></li>' if page < PAGES else ""
    head = "" if base is None else f'<head><base href="{base}"></head>'
    return f"<html>{head}<body>{articles}{first}<ul>{following}</ul></body></html>"


def _book_page(n: int) -> str:
    return f'<html><body><h1>Book {n}</h1><p class="price_color">£{n}.00</p></body></html>'


HTML = [("Content-Type", "text/html; charset=utf-8")]


@pytest.fixture
def bookstore(server: LoopbackServer) -> LoopbackServer:
    """A small paginated catalogue: list pages link to detail pages and to each other."""
    for page in range(1, PAGES + 1):
        server.route(f"/catalogue/page-{page}.html", _list_page(page), headers=HTML)
    for n in range(1, PAGES * BOOKS_PER_PAGE + 1):
        server.route(f"/book/{n}.html", _book_page(n), headers=HTML)
    return server


class Books(Spider):
    """The example from docs/design.md §4.1."""

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


async def test_relative_links_resolve_against_the_final_url_after_a_redirect(
    bookstore: LoopbackServer, tmp_path: Path
) -> None:
    bookstore.route("/start", status=302, headers=[("Location", FIRST_PAGE)])

    class LocalBooks(Books):
        start_urls = (bookstore.url("/start"),)

    output = tmp_path / "books.jsonl"
    async with asyncio.timeout(10):
        await LocalBooks.crawl(output=output)

    assert _sorted(_read_items(output)) == _sorted(EXPECTED_BOOKS)


async def test_relative_links_honor_the_base_href(bookstore: LoopbackServer, tmp_path: Path) -> None:
    bookstore.route("/mirror/index.html", _list_page(1, base="/catalogue/"), headers=HTML)

    class LocalBooks(Books):
        start_urls = (bookstore.url("/mirror/index.html"),)

    output = tmp_path / "books.jsonl"
    async with asyncio.timeout(10):
        await LocalBooks.crawl(output=output)

    assert _sorted(_read_items(output)) == _sorted(EXPECTED_BOOKS)
    assert "/mirror/page-2.html" not in [request.path for request in bookstore.received]


async def test_a_relative_url_in_start_is_an_error(bookstore: LoopbackServer, caplog: pytest.LogCaptureFixture) -> None:
    class Relative(Books):
        start_urls = (FIRST_PAGE,)

    async with asyncio.timeout(10):
        await Relative.crawl()

    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "start() of spider 'books' failed" in record.getMessage()
    assert record.exc_info is not None
    assert "is a relative URL" in str(record.exc_info[1])
    assert bookstore.received == []


async def test_crawl_runs_inside_an_event_loop(bookstore: LoopbackServer, tmp_path: Path) -> None:
    class LocalBooks(Books):
        start_urls = (bookstore.url(FIRST_PAGE),)

    output = tmp_path / "books.jsonl"
    async with asyncio.timeout(10):
        await LocalBooks.crawl(output=output, settings={"concurrency": 2})

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

        async def parse(self, response: Response) -> list[dict[str, str | None]]:
            return [{"text": text} for text in response.css("p::text").getall()]

    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        await Paragraphs.crawl(output=output)

    assert _read_items(output) == [{"text": "a"}, {"text": "b"}]


@pytest.mark.parametrize("concurrency", [0, -1])
async def test_invalid_concurrency_fails_before_touching_the_output(tmp_path: Path, concurrency: int) -> None:
    output = tmp_path / "items.jsonl"
    output.write_text("previous run\n")

    class Anything(Spider):
        start_urls = ("http://127.0.0.1:9/",)

    with pytest.raises(SettingsError, match=r"concurrency: Expected `int` >= 1"):
        await Anything.crawl(output=output, settings={"concurrency": concurrency})

    assert output.read_text() == "previous run\n"


async def test_item_storage_failure_ends_the_run(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "ok")

    class Unserializable(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"tags": {"a", "b"}}  # a set is not JSON-serializable

    errors: list[tuple[Any, str, type[Exception]]] = []

    class ItemErrors:
        @events.on(events.ItemError)
        def failed(self, event: events.ItemError) -> None:
            errors.append((event.item, event.response.url, type(event.error)))

    with pytest.RaisesGroup(TypeError):
        async with asyncio.timeout(10):
            await Unserializable.crawl(output=tmp_path / "items.jsonl", settings={"extensions": {ItemErrors(): 1}})

    assert errors == [({"tags": {"a", "b"}}, server.url("/"), TypeError)]


async def test_missing_parse_is_logged_and_run_continues(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/", "ok")

    class Unfinished(Spider):
        start_urls = (server.url("/"),)

    async with asyncio.timeout(10):
        await Unfinished.crawl()

    assert any("Unfinished.parse() is not implemented" in (r.exc_text or "") for r in caplog.records)


async def test_unsupported_outputs_are_logged(server: LoopbackServer, caplog: pytest.LogCaptureFixture) -> None:
    server.route("/", "ok")

    class Odd(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield 42

    async with asyncio.timeout(10):
        await Odd.crawl()

    assert any("unsupported output 42" in r.getMessage() for r in caplog.records)


class Observer:
    """Records the Request, Response, and Item Events of a Run; vetoes offsite Requests."""

    def __init__(self, allowed_host: str) -> None:
        self.allowed_host = allowed_host
        self.log: list[str] = []

    @events.on(events.RequestScheduled)
    def schedule(self, event: events.RequestScheduled) -> events.Veto | None:
        return None if self.allowed_host in event.request.url else events.Veto("offsite")

    @events.on(events.RequestDropped)
    def dropped(self, event: events.RequestDropped) -> None:
        self.log.append(f"dropped {event.request.url.rsplit('/', 1)[-1]} {event.reason}")

    @events.on(events.ResponseReceived)
    def received(self, event: events.ResponseReceived) -> None:
        self.log.append(f"received {event.response.url.rsplit('/', 1)[-1]}")

    @events.on(events.ItemScraped)
    def scraped(self, event: events.ItemScraped) -> None:
        self.log.append(f"item {event.item['n']} from {event.response.url.rsplit('/', 1)[-1]}")

    @events.on(events.RequestFailed)
    def failed(self, event: events.RequestFailed) -> None:
        self.log.append(f"failed {event.request.url.rsplit('/', 1)[-1]} {type(event.error).__name__}")


async def test_the_engine_emits_request_response_and_item_events(server: LoopbackServer) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    server.route("/start", "ok")
    server.route("/broken", "ok")
    observer = Observer(allowed_host="127.0.0.1")

    class Eventful(Spider):
        start_urls = (server.url("/start"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"n": 1}
            yield Request("https://elsewhere.example/offsite")
            yield Request(server.url("/start"))  # a duplicate
            yield Request(server.url("/broken"), callback=self.parse_broken)
            yield Request(f"http://127.0.0.1:{closed_port}/unreachable")

        async def parse_broken(self, response: Response) -> AsyncIterator[Any]:
            raise ValueError("bad page")
            yield

    async with asyncio.timeout(10):
        await Eventful.crawl(settings={"concurrency": 1, "extensions": {observer: 1}})

    assert observer.log[:4] == [
        "received start",
        "item 1 from start",
        "dropped offsite offsite",
        "dropped start duplicate",
    ]
    assert sorted(observer.log[4:]) == [
        "failed broken ValueError",
        "failed unreachable ConnectionError",
        "received broken",
    ]


async def test_a_self_cancelled_item_error_subscriber_does_not_hide_the_storage_failure(
    server: LoopbackServer, tmp_path: Path
) -> None:
    server.route("/", "ok")
    server.route("/next", "ok")

    class CancelsItself:
        @events.on(events.ItemError)
        async def failed(self, event: events.ItemError) -> None:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            future.cancel()
            await future

    class Unserializable(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield Request(server.url("/next"), callback=self.parse_next)
            yield {"tags": {"a"}}

        async def parse_next(self, response: Response) -> AsyncIterator[Any]:
            yield {"ok": True}

    settings = {"concurrency": 1, "extensions": {CancelsItself(): 1}}
    with pytest.RaisesGroup(TypeError):
        async with asyncio.timeout(10):
            await Unserializable.crawl(output=tmp_path / "items.jsonl", settings=settings)


async def test_callbacks_receive_the_request_kwargs(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "ok")
    server.route("/item", "item")

    class Catalogue(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield Request(server.url("/item"), callback=self.parse_item, kwargs={"category": "books", "page": 2})

        async def parse_item(self, response: Response, category: str, page: int) -> AsyncIterator[Any]:
            yield {"category": category, "page": page, "body": response.text}

    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        await Catalogue.crawl(output=output)

    assert _read_items(output) == [{"category": "books", "page": 2, "body": "item"}]


async def test_dont_filter_bypasses_deduplication(server: LoopbackServer) -> None:
    server.route("/", "ok")
    server.route("/again", "again")

    class Repeat(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/"):
                yield Request(server.url("/again"))
                yield Request(server.url("/again"))
                yield Request(server.url("/again"), dont_filter=True)

    async with asyncio.timeout(10):
        await Repeat.crawl()

    assert [r.path for r in server.received].count("/again") == 2


def _item_links(server: LoopbackServer, *hrefs: str) -> None:
    links = "".join(f"<a href='{href}'>x</a>" for href in hrefs)
    server.route("/", f"<html><body>{links}</body></html>", headers=HTML)
    server.route("/item", "<p>item</p>", headers=HTML)


class Idle(Spider):
    start_urls = ()


class Items(Spider):
    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for href in response.css("a::attr(href)").getall():
            yield Request(href, callback=self.parse_item)

    async def parse_item(self, response: Response) -> None:
        pass


@pytest.mark.parametrize(("settings", "fetches"), [({}, 1), ({"fingerprint.sort_query": False}, 2)])
async def test_query_order_counts_only_when_sorting_is_off(
    server: LoopbackServer, settings: dict[str, Any], fetches: int
) -> None:
    _item_links(server, "/item?a=1&b=2#top", "/item?b=2&a=1")

    class Local(Items):
        start_urls = (server.url("/"),)

    async with asyncio.timeout(10):
        await Local.crawl(settings=settings)

    assert [request.path for request in server.received].count("/item") == fetches


async def test_a_fingerprint_function_replaces_the_default(server: LoopbackServer) -> None:
    _item_links(server, "/item?id=1", "/item?id=2")

    class Local(Items):
        start_urls = (server.url("/"),)

    async with asyncio.timeout(10):
        await Local.crawl(settings={"fingerprint.function": lambda request: request.url.partition("?")[0]})

    assert [request.path for request in server.received].count("/item") == 1


async def test_fingerprint_settings_are_checked_before_the_run() -> None:
    with pytest.raises(SettingsError, match="the Cookie header cannot be declared"):
        await Idle.crawl(settings={"fingerprint.headers": ["Accept-Language", "Cookie"]})
    with pytest.raises(ComponentError, match=r"fingerprint\.function must be callable"):
        await Idle.crawl(settings={"fingerprint.function": 42})
