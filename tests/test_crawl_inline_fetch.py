"""Inline Fetches in whole crawls against the loopback server."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import orjson
import pytest

from pernix import Middleware, Request, Response, Spider, middleware
from pernix.crawler import Crawler
from pernix.errors import RetriesExhausted
from pernix.testing import Reply

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from pernix.middlewares import Download
    from pernix.run_result import RunResult
    from pernix.testing import LoopbackServer, ReceivedRequest

SETTINGS: Mapping[str, Any] = {"stats.interval": 0, "retry.delay_scale": 0}


def _crawler(spider: type[Spider], output: Path, **settings: Any) -> Crawler:
    return Crawler.from_spider(spider, settings={**SETTINGS, **settings}, output=output)


async def _crawl(spider: type[Spider], tmp_path: Path, **settings: Any) -> tuple[list[Any], RunResult]:
    output = tmp_path / "items.jsonl"
    async with asyncio.timeout(10):
        result = await _crawler(spider, output, **settings).crawl()
    return [orjson.loads(line) for line in output.read_text().splitlines()], result


def _path(url: str) -> str:
    return urlsplit(url).path


@pytest.fixture
def members(server: LoopbackServer) -> LoopbackServer:
    """A site whose account page greets members, who log in with a POST that sets a cookie."""

    def login(request: ReceivedRequest) -> Reply:
        if request.body != b"user=me&password=secret":
            return Reply(status=403)
        return Reply(body="welcome", headers=[("Set-Cookie", "sid=s3cret; Path=/")])

    def account(request: ReceivedRequest) -> Reply:
        member = "sid=s3cret" in (request.header("Cookie") or "")
        return Reply(body=f"<h1>{'Me' if member else 'Guest'}</h1>")

    server.route("/", "<p>home</p>")
    server.route("/login", method="POST", handler=login)
    server.route("/account", handler=account)
    return server


async def test_a_callback_logs_in_then_fetches_as_the_member(members: LoopbackServer, tmp_path: Path) -> None:
    class Member(Spider):
        start_urls = (members.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            login = await self.fetch(Request("/login", method="POST", data={"user": "me", "password": "secret"}))
            guest = await self.fetch("/account")
            member = await self.fetch(Request("/account", cookies=login.cookies))
            yield {"guest": guest.css("h1::text").get(), "member": member.css("h1::text").get()}

    items, result = await _crawl(Member, tmp_path)

    assert items == [{"guest": "Guest", "member": "Me"}]
    assert [(request.method, request.path) for request in members.received] == [
        ("GET", "/"),
        ("POST", "/login"),
        ("GET", "/account"),
        ("GET", "/account"),
    ]
    stats = result.stats
    assert (stats["requests.enqueued"], stats["frontier.done"]) == (1, 1)  # Inline Fetches bypass the Frontier
    assert (stats["downloads.responses"], stats["responses.received"]) == (4, 1)


async def test_start_may_log_in_before_yielding_requests(members: LoopbackServer, tmp_path: Path) -> None:
    class LoggedIn(Spider):
        async def start(self) -> AsyncIterator[Request]:
            login = await self.fetch(
                Request(members.url("/login"), method="POST", data={"user": "me", "password": "secret"})
            )
            yield Request(members.url("/account"), cookies=login.cookies)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"name": response.css("h1::text").get()}

    items, _ = await _crawl(LoggedIn, tmp_path)

    assert items == [{"name": "Me"}]


async def test_a_callback_pages_through_an_api_in_order(server: LoopbackServer, tmp_path: Path) -> None:
    def api(request: ReceivedRequest) -> Reply:
        page = int(dict(request.query)["page"])
        data = {"names": [f"{page}a", f"{page}b"], "next": page + 1 if page < 3 else None}
        return Reply(body=orjson.dumps(data), headers=[("Content-Type", "application/json")])

    server.route("/", "<p>home</p>")
    server.route("/api", handler=api)

    class Paging(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page: int | None = 1
            while page is not None:
                data = (await self.fetch(f"/api?page={page}")).json()
                for name in data["names"]:
                    yield {"name": name}
                page = data["next"]

    items, _ = await _crawl(Paging, tmp_path)

    assert [item["name"] for item in items] == ["1a", "1b", "2a", "2b", "3a", "3b"]
    assert [request.query for request in server.received[1:]] == [[("page", "1")], [("page", "2")], [("page", "3")]]


async def test_inline_fetches_are_retried_in_place(server: LoopbackServer, tmp_path: Path) -> None:
    replies = [Reply(status=503), Reply(status=500), Reply(body="ok")]
    server.route("/", "<p>home</p>")
    server.route("/flaky", handler=lambda _request: replies.pop(0))
    server.route("/down", status=503)

    class Flaky(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            flaky = await self.fetch("/flaky")
            try:
                await self.fetch("/down")
            except RetriesExhausted as exc:
                yield {"flaky": flaky.text, "down": exc.response.status if exc.response else None}

    items, result = await _crawl(Flaky, tmp_path, **{"retry.times": 2})

    assert items == [{"flaky": "ok", "down": 503}]
    assert [request.path for request in server.received] == ["/"] + ["/flaky"] * 3 + ["/down"] * 3
    stats = result.stats
    assert (stats["requests.retried"], stats["frontier.done"], stats["requests.failed"]) == (4, 1, 0)


class _Slow:
    """A page that answers only once released, noting when a request arrives."""

    def __init__(self, server: LoopbackServer) -> None:
        self.arrived = threading.Event()
        self.release = threading.Event()
        server.route("/", "<p>home</p>")
        server.route("/slow", handler=self._answer)

    def _answer(self, _request: ReceivedRequest) -> Reply:
        self.arrived.set()
        self.release.wait(10)
        return Reply(body="late")


def _waiting_spider(server: LoopbackServer) -> type[Spider]:
    class Waiting(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            late = await self.fetch("/slow")
            yield {"late": late.text}

    return Waiting


async def test_a_force_stop_cancels_the_inline_fetch_in_flight(server: LoopbackServer, tmp_path: Path) -> None:
    slow = _Slow(server)
    crawler = _crawler(_waiting_spider(server), tmp_path / "items.jsonl")
    run = asyncio.create_task(crawler.crawl())
    try:
        async with asyncio.timeout(5):
            await asyncio.to_thread(slow.arrived.wait, 5)
            crawler.stop("force")
            result = await run
    finally:
        slow.release.set()

    stats = result.stats
    assert result.reason == "forced"
    assert (stats["frontier.ready"], stats["frontier.done"], stats["requests.retried"]) == (1, 0, 0)
    assert (tmp_path / "items.jsonl").read_text() == ""


async def test_a_drain_lets_the_inline_fetch_in_flight_finish(server: LoopbackServer, tmp_path: Path) -> None:
    slow = _Slow(server)
    crawler = _crawler(_waiting_spider(server), tmp_path / "items.jsonl")
    run = asyncio.create_task(crawler.crawl())
    try:
        async with asyncio.timeout(5):
            await asyncio.to_thread(slow.arrived.wait, 5)
            crawler.stop()
            slow.release.set()
            result = await run
    finally:
        slow.release.set()

    assert result.reason == "drained"
    assert (tmp_path / "items.jsonl").read_text() == '{"late":"late"}\n'


async def test_inline_fetches_go_through_the_download_middleware_with_the_session(
    server: LoopbackServer, tmp_path: Path
) -> None:
    seen: list[tuple[str, str | None]] = []

    @middleware
    async def record(request: Request, call_next: Download) -> Response:
        seen.append((_path(request.url), request.session))
        return await call_next(request)

    server.route("/", "<p>home</p>")

    class Sessions(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(server.url("/"), session="alice")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            await self.fetch("/a")
            await self.fetch(Request("/b", session="bob"))
            yield {"done": True}

    items, _ = await _crawl(Sessions, tmp_path, middlewares={record: 100})

    assert items == [{"done": True}]
    assert seen == [("/", "alice"), ("/a", "alice"), ("/b", "bob")]


async def test_a_middleware_may_fetch_a_token_from_the_domain_it_is_fetching(
    server: LoopbackServer, tmp_path: Path
) -> None:
    server.route("/token", "t0ken")
    server.route("/", handler=lambda request: Reply(body=f"<p>{request.header('Authorization')}</p>"))

    class Authorize(Middleware):
        def __init__(self, crawler: Crawler) -> None:
            self.crawler = crawler

        async def __call__(self, request: Request, call_next: Download) -> Response:
            if _path(request.url) == "/token":
                return await call_next(request)
            token = await self.crawler.fetch(server.url("/token"))
            return await call_next(request.replace(headers=[("Authorization", token.text)]))

    class Home(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"authorization": response.css("p::text").get()}

    output = tmp_path / "items.jsonl"
    crawler = _crawler(Home, output, **{"domain.concurrency": 1, "middlewares": {Authorize: 100}})
    async with asyncio.timeout(5):  # the token fetch shares the slot the page fetch holds
        await crawler.crawl()

    assert output.read_text() == '{"authorization":"t0ken"}\n'
    assert [request.path for request in server.received] == ["/token", "/"]


async def test_a_callback_streams_a_download_to_a_file(server: LoopbackServer, tmp_path: Path) -> None:
    body = bytes(range(256)) * 8192
    server.route("/", "<p>home</p>")
    server.route("/file.bin", body, headers=[("Content-Type", "application/octet-stream")])
    seen: list[tuple[str, bool, int]] = []

    @middleware
    async def record(request: Request, call_next: Download) -> Response:
        response = await call_next(request)
        seen.append((_path(response.url), response.streamed, len(response.body)))
        return response

    class Downloads(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            async with self.stream("/file.bin", max_bytes=0) as file:
                size = await file.save(tmp_path / "file.bin")
            yield {"size": size, "type": file.response.headers.get("Content-Type")}

    items, result = await _crawl(Downloads, tmp_path, middlewares={record: 100})

    assert items == [{"size": len(body), "type": "application/octet-stream"}]
    assert (tmp_path / "file.bin").read_bytes() == body
    assert seen == [("/", False, len(b"<p>home</p>")), ("/file.bin", True, 0)]
    assert result.stats["downloads.bytes"] == len(b"<p>home</p>") + len(body)


async def test_inline_fetches_need_a_running_run(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "<p>home</p>")

    class Home(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> None:
            pass

    crawler = _crawler(Home, tmp_path / "items.jsonl")
    spider = crawler.spider
    with pytest.raises(RuntimeError, match="Inline Fetches work only while the Run runs"):
        await spider.fetch(server.url("/"))
    async with asyncio.timeout(10):
        await crawler.crawl()

    with pytest.raises(RuntimeError, match="Inline Fetches work only while the Run runs"):
        spider.stream(server.url("/"))
