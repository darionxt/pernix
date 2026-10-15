"""End-to-end crawls with Middleware and Fetchers registered through the settings."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, Self

import pytest

from pernix import Extension, Request, Response, Spider, events
from pernix.errors import ComponentError, IgnoreRequest, SettingsError
from pernix.fetchers import HTTPFetcher
from pernix.middlewares import Middleware, SpiderMiddleware, middleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix.middlewares import Download, HandleResponse
    from pernix.settings import Settings
    from pernix.testing import LoopbackServer

HTML = [("Content-Type", "text/html; charset=utf-8")]


async def _crawl(spider: type[Spider], **settings: Any) -> None:
    async with asyncio.timeout(10):
        await spider.crawl(settings=settings)


async def test_registered_components_shape_the_crawl_and_follow_the_run(server: LoopbackServer) -> None:
    server.route("/a", "<a href='b'>b</a>", headers=HTML)
    server.route("/b", "<p>b</p>", headers=HTML)
    log: list[str] = []
    items: list[Any] = []

    class Recording:
        name = ""

        async def open(self) -> None:
            log.append(f"open {self.name}")

        async def close(self) -> None:
            log.append(f"close {self.name}")

    class Watch(Recording, Extension):
        name = "extension"

        @events.on(events.ItemScraped)
        def scraped(self, event: events.ItemScraped) -> None:
            items.append(event.item)

    class LoggingHTTP(HTTPFetcher):
        async def __aenter__(self) -> Self:
            log.append("open fetcher")
            return await super().__aenter__()

        async def __aexit__(self, *exc: object) -> None:
            log.append("close fetcher")
            await super().__aexit__(*exc)

    class Tag(Recording, Middleware):
        name = "middleware"

        def __init__(self, settings: Settings) -> None:
            self.tag = str(settings["concurrency"])

        async def __call__(self, request: Request, call_next: Download) -> Response:
            return await call_next(request.replace(headers={"X-Tag": self.tag}))

        @events.on(events.ResponseReceived)
        def received(self, event: events.ResponseReceived) -> None:
            log.append(f"received {event.response.url.rsplit('/', 1)[1]}")

    class Stamp(Recording, SpiderMiddleware):
        name = "spider middleware"

        async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
            async for output in call_next(response):
                yield {**output, "fetcher": response.fetcher} if isinstance(output, dict) else output

    class Pages(Spider):
        start_urls = (server.url("/a"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"page": response.url.rsplit("/", 1)[1]}
            for href in response.css("a::attr(href)").getall():
                yield Request(href)

    await _crawl(
        Pages,
        concurrency=1,
        extensions={Watch: 1},
        fetchers={"http": LoggingHTTP},
        middlewares={Tag: 1},
        spider_middlewares={Stamp: 1},
    )

    assert [request.header("X-Tag") for request in server.received] == ["1", "1"]
    assert items == [{"page": "a", "fetcher": "http"}, {"page": "b", "fetcher": "http"}]
    assert log == [
        "open extension",
        "open fetcher",
        "open middleware",
        "open spider middleware",
        "received a",
        "received b",
        "close spider middleware",
        "close middleware",
        "close fetcher",
        "close extension",
    ]


async def test_ignored_requests_are_reported_and_never_parsed(server: LoopbackServer) -> None:
    server.route("/a", "<a href='skip'>skip</a><a href='b'>b</a>", headers=HTML)
    server.route("/b", "<p>b</p>", headers=HTML)
    seen: list[str] = []

    @middleware
    async def skip(request: Request, call_next: Download) -> Response:
        if request.url.endswith("/skip"):
            raise IgnoreRequest("not needed")
        return await call_next(request)

    class Watch(Extension):
        @events.on(events.RequestIgnored)
        def ignored(self, event: events.RequestIgnored) -> None:
            seen.append(f"ignored {event.request.url.rsplit('/', 1)[1]}: {event.reason}")

        @events.on(events.RequestFailed)
        def failed(self, event: events.RequestFailed) -> None:
            seen.append(f"failed {event.request.url}")

    class Pages(Spider):
        start_urls = (server.url("/a"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            seen.append(f"parsed {response.url.rsplit('/', 1)[1]}")
            for href in response.css("a::attr(href)").getall():
                yield Request(href)

    await _crawl(Pages, concurrency=1, extensions={Watch: 1}, middlewares={skip: 1})

    assert seen == ["parsed a", "ignored skip: not needed", "parsed b"]
    assert [request.path for request in server.received] == ["/a", "/b"]


class Memory:
    """A Fetcher of `mem://` pages held in memory."""

    schemes: ClassVar[frozenset[str]] = frozenset({"mem"})
    pages: ClassVar[dict[str, str]] = {
        "mem://site/index.html": "<a href='mem://site/2.html'>2</a><a href='https://example.invalid/3'>3</a>",
        "mem://site/2.html": "<p>2</p>",
        "https://example.invalid/3": "<p>3</p>",
    }

    async def fetch(self, request: Request) -> Response:
        return Response(
            url=request.url, status=200, headers=HTML, body=self.pages[request.url].encode(), request=request
        )


async def test_custom_fetchers_are_selected_by_scheme_or_by_name() -> None:
    fetched: list[tuple[str, str | None]] = []

    class Site(Spider):
        start_urls = ("mem://site/index.html",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            fetched.append((response.url, response.fetcher))
            for href in response.css("a::attr(href)").getall():
                yield Request(href, fetcher="memory" if href.startswith("https:") else None)

    await _crawl(Site, fetchers={"memory": Memory})

    assert sorted(fetched) == [
        ("https://example.invalid/3", "memory"),
        ("mem://site/2.html", "memory"),
        ("mem://site/index.html", "memory"),
    ]


async def test_relative_links_of_responses_with_other_schemes_fail_the_callback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Site(Spider):
        start_urls = ("mem://site/index.html",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield Request("2.html")

    await _crawl(Site, fetchers={"memory": Memory})

    assert "the relative URL '2.html' cannot be resolved" in caplog.text


class Opened(Extension):
    opened: ClassVar[list[str]] = []

    async def open(self) -> None:
        self.opened.append("opened")


class Blocking:
    def __call__(self, request: Request, call_next: Download) -> Response:
        raise NotImplementedError


class Idle(Spider):
    start_urls = ()


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"middlewares": {Blocking: 1}}, ComponentError),
        ({"spider_middlewares": {Blocking: 1}}, ComponentError),
        ({"fetchers": {"memory": Memory, "other": Memory}}, SettingsError),
        ({"fetchers": {"memory": object()}}, ComponentError),
    ],
)
async def test_misfitting_components_fail_before_anything_opens(
    settings: dict[str, Any], error: type[Exception]
) -> None:
    Opened.opened.clear()

    with pytest.raises(error):
        await _crawl(Idle, extensions={Opened: 1}, **settings)

    assert Opened.opened == []
