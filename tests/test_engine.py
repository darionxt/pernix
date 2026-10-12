"""The Engine against an in-memory Frontier and a fake download chain, without a network."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from pernix import IgnoreRequest, Request, Response, Spider, events
from pernix.engine import Engine, run_callbacks
from pernix.frontiers import MemoryFrontier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

SITE = "https://site.example"


class FakeSite:
    """A download chain answering from a dict of pages; a missing page fails."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def __call__(self, request: Request) -> Response:
        path = request.url.removeprefix(SITE)
        self.fetched.append(path)
        if path == "/ignored":
            raise IgnoreRequest("not needed")
        body = self.pages[path]
        return Response(
            url=request.url, status=200, headers=[("Content-Type", "text/html")], body=body.encode(), request=request
        )


async def _run(
    spider: Spider, site: FakeSite, *, concurrency: int = 4, lease_ttl: float = 60.0
) -> tuple[MemoryFrontier, list[Mapping[str, Any]], events.EventBus]:
    frontier = MemoryFrontier()
    items: list[Mapping[str, Any]] = []
    bus = events.EventBus(timeout=5)
    engine = Engine(
        spider,
        frontier=frontier,
        download=site,
        handle=run_callbacks(spider),
        item_sink=items.append,
        events=bus,
        concurrency=concurrency,
        lease_ttl=lease_ttl,
    )
    async with asyncio.timeout(10):
        await engine.run()
    return frontier, items, bus


class Links(Spider):
    """Follows every link and yields the page's paragraph."""

    start_urls = (f"{SITE}/a",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"page": response.css("p::text").get()}
        for href in response.css("a::attr(href)").getall():
            yield Request(href)


async def test_requests_added_before_their_parent_is_settled_keep_the_run_going() -> None:
    class Slow(Links):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            await asyncio.sleep(0.1)  # the other workers are idle meanwhile
            async for output in super().parse(response):
                yield output

    site = FakeSite({"/a": "<p>a</p><a href='/b'>b</a>", "/b": "<p>b</p><a href='/c'>c</a>", "/c": "<p>c</p>"})

    frontier, items, _ = await _run(Slow(), site)

    assert items == [{"page": "a"}, {"page": "b"}, {"page": "c"}]
    assert (await frontier.counts()).done == 3


async def test_outcomes_settle_the_leases() -> None:
    class Mixed(Links):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/broken"):
                raise ValueError("cannot parse")
            async for output in super().parse(response):
                yield output

    site = FakeSite({"/a": "<a href='/ignored'></a><a href='/missing'></a><a href='/broken'></a>", "/broken": ""})

    frontier, _, _ = await _run(Mixed(), site)

    counts = await frontier.counts()
    assert (counts.done, counts.dead, counts.unfinished) == (2, 2, 0)


async def test_on_idle_runs_until_it_adds_nothing_new() -> None:
    calls: list[int] = []

    class MoreOnIdle(Links):
        async def on_idle(self) -> AsyncIterator[Request]:
            calls.append(len(calls) + 1)
            yield Request(f"{SITE}/later")  # new the first time, a duplicate afterwards

    site = FakeSite({"/a": "<p>a</p>", "/later": "<p>later</p>"})
    idle: list[str] = []

    spider = MoreOnIdle()
    frontier = MemoryFrontier()
    items: list[Mapping[str, Any]] = []
    bus = events.EventBus(timeout=5)
    bus.on(events.SpiderIdle, lambda event: idle.append(event.spider.name))
    engine = Engine(
        spider, frontier=frontier, download=site, handle=run_callbacks(spider), item_sink=items.append, events=bus,
        concurrency=2,
    )  # fmt: skip
    async with asyncio.timeout(10):
        await engine.run()

    assert items == [{"page": "a"}, {"page": "later"}]
    assert calls == [1, 2]
    assert idle == ["links", "links"]


async def test_a_failing_on_idle_ends_the_run(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(Links):
        async def on_idle(self) -> AsyncIterator[Request]:
            yield Request("/relative")

    await _run(Broken(), FakeSite({"/a": "<p>a</p>"}))

    assert "on_idle() of spider 'links' failed" in caplog.text


async def test_leases_are_renewed_while_a_request_is_processed() -> None:
    class Slow(Links):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            await asyncio.sleep(0.3)  # several times the Lease's lifetime
            async for output in super().parse(response):
                yield output

    site = FakeSite({"/a": "<p>a</p>"})

    frontier, items, _ = await _run(Slow(), site, lease_ttl=0.06)

    assert site.fetched == ["/a"]
    assert items == [{"page": "a"}]
    assert (await frontier.counts()).done == 1


@pytest.mark.parametrize(("concurrency", "lease_ttl"), [(0, 60.0), (1, 0.0)])
def test_invalid_engine_settings_are_rejected(concurrency: int, lease_ttl: float) -> None:
    spider = Links()

    with pytest.raises(ValueError, match="must be"):
        Engine(
            spider,
            frontier=MemoryFrontier(),
            download=FakeSite({}),
            handle=run_callbacks(spider),
            item_sink=lambda item: None,
            events=events.EventBus(),
            concurrency=concurrency,
            lease_ttl=lease_ttl,
        )
