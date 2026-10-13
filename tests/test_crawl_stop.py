"""Stopping whole crawls against the loopback server: Drain, Force Stop, and the Run's result."""

from __future__ import annotations

import asyncio
import contextvars
from typing import TYPE_CHECKING, Any, Self

import orjson
import pytest

from pernix import Pipeline, Request, Response, Spider, events
from pernix.crawler import Crawler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from loopback import LoopbackServer
    from pernix.items import ItemContext
    from pernix.shutdown import StopMode


def _lines(path: Path) -> list[Any]:
    return [orjson.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def pages(server: LoopbackServer) -> LoopbackServer:
    for n in range(1, 6):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    return server


def _spider(server: LoopbackServer, entered: asyncio.Event, gate: asyncio.Event) -> type[Spider]:
    """A Spider over five pages whose first Callback waits for `gate` once it has `entered`."""

    class Pages(Spider):
        start_urls = tuple(server.url(f"/{n}") for n in range(1, 6))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            entered.set()
            await gate.wait()
            yield {"page": response.css("h1::text").get()}
            yield Request(server.url("/later"))

    return Pages


def _crawler(spider: type[Spider], settings: Mapping[str, Any] | None = None, **options: Any) -> Crawler:
    return Crawler.from_spider(spider, settings={"concurrency": 1, "stats.interval": 0, **(settings or {})}, **options)


async def test_a_drain_finishes_the_request_in_flight_and_fetches_no_more(
    pages: LoopbackServer, tmp_path: Path
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    crawler = _crawler(_spider(pages, entered, gate), output=tmp_path / "items.jsonl")
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop()
        gate.set()
        result = await run

    assert (result.run_id, result.reason) == (crawler.run_id, "drained")
    assert closed == ["drained"]
    assert [request.path for request in pages.received] == ["/1"]
    assert _lines(tmp_path / "items.jsonl") == [{"page": "1"}]
    stats = result.stats
    assert (stats["run.reason"], stats["frontier.done"], stats["frontier.ready"]) == ("drained", 1, 5)


async def test_a_spider_may_stop_its_own_run(pages: LoopbackServer) -> None:
    class First(Spider):
        start_urls = tuple(pages.url(f"/{n}") for n in range(1, 6))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.crawler.stop()
            yield {"page": response.css("h1::text").get()}

    async with asyncio.timeout(10):
        result = await _crawler(First).crawl()

    assert result.reason == "drained"
    assert (result.stats["items.scraped"], len(pages.received)) == (1, 1)


async def test_a_force_stop_returns_the_request_in_flight_and_discards_the_queued_items(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    processing = asyncio.Event()

    class Slow(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            processing.set()
            await asyncio.Event().wait()
            return item

    class Many(Spider):
        start_urls = (pages.url("/1"), pages.url("/2"))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(3):
                yield {"n": n}
            await asyncio.Event().wait()  # still in flight when stopped
            yield {"n": "never"}

    crawler = _crawler(Many, {"pipelines": {Slow: 100}}, output=tmp_path / "items.jsonl")
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await processing.wait()
        await asyncio.sleep(0.01)  # the other Items are queued
        crawler.stop("force")
        result = await run

    stats = result.stats
    assert result.reason == "forced"
    assert (stats["items.discarded"], stats["items.scraped"]) == (3, 0)
    assert (stats["frontier.ready"], stats["frontier.leased"], stats["frontier.done"]) == (2, 0, 0)
    assert (tmp_path / "items.jsonl").read_text() == ""  # the Feed is still closed
    assert "Stopping the Run now: cancelling the work in flight" in caplog.text


async def test_the_returned_request_keeps_its_attempt(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(_spider(pages, entered, asyncio.Event()))
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("force")
        result = await run

    assert result.stats["requests.retried"] == 0
    assert result.stats["frontier.ready"] == 5


async def test_a_drain_past_its_grace_turns_into_a_force_stop(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(_spider(pages, entered, asyncio.Event()), {"shutdown.grace": 0.05})
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("drain")
        result = await run

    assert result.reason == "forced"
    assert closed == ["forced"]


class SlowClose:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def close(self) -> None:
        await asyncio.sleep(self.seconds)


async def test_closing_after_a_force_stop_is_limited(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(
        _spider(pages, entered, asyncio.Event()),
        {"extensions": {SlowClose(60): 100}, "shutdown.close_timeout": 0.05},
    )
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("force")
        with pytest.raises(ExceptionGroup) as raised:
            await run

    [error] = raised.value.exceptions
    assert str(error) == "closing took longer than 0.05 s"


async def test_closing_after_a_finished_run_is_not_limited(pages: LoopbackServer) -> None:
    gate = asyncio.Event()
    gate.set()
    crawler = _crawler(
        _spider(pages, asyncio.Event(), gate),
        {"extensions": {SlowClose(0.1): 100}, "shutdown.close_timeout": 0.01},
    )

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "finished"


async def test_a_cancelled_run_limits_closing_too(pages: LoopbackServer, caplog: pytest.LogCaptureFixture) -> None:
    entered = asyncio.Event()
    crawler = _crawler(
        _spider(pages, entered, asyncio.Event()),
        {"extensions": {SlowClose(60): 100}, "shutdown.close_timeout": 0.05},
    )
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    assert closed == ["cancelled"]
    assert "closing took longer than 0.05 s" in caplog.text


async def test_a_stop_requested_before_the_run_takes_effect_when_it_starts(pages: LoopbackServer) -> None:
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()))

    crawler.stop()
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "drained"
    assert pages.received == []


async def test_a_stop_after_the_run_does_nothing(pages: LoopbackServer) -> None:
    gate = asyncio.Event()
    gate.set()
    crawler = _crawler(_spider(pages, asyncio.Event(), gate))
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    crawler.stop("force")

    assert result.reason == "finished"


def test_stop_modes_are_checked(pages: LoopbackServer) -> None:
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()))

    with pytest.raises(ValueError, match="the stop mode must be 'drain' or 'force'"):
        crawler.stop("pause")  # pyright: ignore[reportArgumentType]


class Recorded:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name, self.calls = name, calls

    async def open(self) -> None:
        self.calls.append(f"open {self.name}")

    async def close(self) -> None:
        self.calls.append(f"close {self.name}")


class StuckOpening(Recorded):
    def __init__(self, name: str, calls: list[str], entered: asyncio.Event) -> None:
        super().__init__(name, calls)
        self.entered = entered

    async def open(self) -> None:
        await super().open()
        self.entered.set()
        await asyncio.Event().wait()


class StuckClosing(Recorded):
    def __init__(self, name: str, calls: list[str], entered: asyncio.Event) -> None:
        super().__init__(name, calls)
        self.entered = entered

    async def close(self) -> None:
        await super().close()
        self.entered.set()
        await asyncio.Event().wait()


def _opened_and_closed(crawler: Crawler) -> list[str]:
    seen: list[str] = []
    crawler.events.on(events.SpiderOpened, lambda _event: seen.append("opened"))
    crawler.events.on(events.SpiderClosed, lambda event: seen.append(event.reason))
    return seen


@pytest.mark.parametrize(("mode", "settings"), [("force", {}), ("drain", {"shutdown.grace": 0.05})])
async def test_a_stop_interrupts_components_that_are_opening(
    pages: LoopbackServer, mode: StopMode, settings: dict[str, Any]
) -> None:
    calls: list[str] = []
    entered = asyncio.Event()
    extensions = {Recorded("first", calls): 100, StuckOpening("stuck", calls, entered): 200}
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()), {"extensions": extensions, **settings})
    seen = _opened_and_closed(crawler)

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop(mode)
        result = await run

    assert result.reason == "forced"
    assert calls == ["open first", "open stuck", "close first"]  # what failed to open is not closed
    assert seen == []  # the Run never started
    assert pages.received == []


async def test_a_force_stop_while_closing_limits_the_close_in_progress(pages: LoopbackServer) -> None:
    calls: list[str] = []
    entered, gate = asyncio.Event(), asyncio.Event()
    gate.set()
    crawler = _crawler(
        _spider(pages, asyncio.Event(), gate),
        {"extensions": {StuckClosing("stuck", calls, entered): 100}, "shutdown.close_timeout": 0.05},
    )

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("force")
        with pytest.raises(ExceptionGroup) as raised:
            await run

    assert [str(error) for error in raised.value.exceptions] == ["closing took longer than 0.05 s"]
    assert crawler.stats.get("run.reason") == "finished"  # the work had ended on its own


async def test_a_drain_whose_closing_outlasts_the_grace_period_limits_it(pages: LoopbackServer) -> None:
    calls: list[str] = []
    entered = asyncio.Event()
    crawler = _crawler(
        _spider(pages, asyncio.Event(), asyncio.Event()),
        {
            "extensions": {StuckClosing("stuck", calls, entered): 100},
            "shutdown.grace": 0.1,
            "shutdown.close_timeout": 0.05,
        },
    )

    crawler.stop()
    with pytest.raises(ExceptionGroup) as raised:
        async with asyncio.timeout(10):
            await crawler.crawl()

    assert [str(error) for error in raised.value.exceptions] == ["closing took longer than 0.05 s"]
    assert crawler.stats.get("run.reason") == "drained"


TENANT: contextvars.ContextVar[str] = contextvars.ContextVar("tenant")


class TenantScope:
    """A component that sets a context variable for the Run and resets it after."""

    def __init__(self) -> None:
        self.token: contextvars.Token[str] | None = None

    async def __aenter__(self) -> Self:
        self.token = TENANT.set("acme")
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self.token is not None
        TENANT.reset(self.token)


async def test_context_set_by_components_while_opening_lasts_until_they_close(pages: LoopbackServer) -> None:
    class Tenanted(Spider):
        start_urls = (pages.url("/1"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"tenant": TENANT.get("none")}

    async with asyncio.timeout(10):
        result = await _crawler(Tenanted, {"extensions": {TenantScope(): 100}}).crawl()

    assert result.reason == "finished"
    assert result.stats["items.scraped"] == 1
    assert TENANT.get("none") == "none"


class StopsWhileOpening:
    """A component that force-stops the Run while it opens, without suspending."""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    async def open(self) -> None:
        self.crawler.stop("force")


async def test_a_force_stop_while_opening_ends_the_run_even_if_opening_never_suspends(
    pages: LoopbackServer,
) -> None:
    calls: list[str] = []
    extensions = {StopsWhileOpening: 100, Recorded("after", calls): 200}
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()), {"extensions": extensions})
    seen = _opened_and_closed(crawler)

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "forced"
    assert calls == ["open after", "close after"]
    assert seen == []
    assert pages.received == []
