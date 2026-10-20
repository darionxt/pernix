"""Pausing the Engine against an in-memory Frontier and a FakeFetcher, without a network."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pernix import Request, Response, Spider, events
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.frontiers import FrontierCounts, MemoryFrontier
from pernix.pausing import Pausing
from pernix.testing import FakeFetcher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Collection

    from pernix.frontiers import Lease
    from pernix.items import ItemContext

SITE = "https://site.example"


class HeldPausing(Pausing):
    """A Pausing that tells how many tasks wait for the Run to be resumed."""

    def __init__(self, spider: Spider, bus: events.EventBus) -> None:
        super().__init__(spider, bus)
        self.waiting = 0
        self._changed = asyncio.Event()

    async def released(self) -> None:
        if not self.held:
            return
        self._count(1)
        try:
            await super().released()
        finally:
            self._count(-1)

    async def waiting_for(self, tasks: int) -> None:
        """Wait until that many tasks, workers and the feeding, wait for the Run to be resumed."""
        while self.waiting < tasks:
            await self._changed.wait()

    def _count(self, change: int) -> None:
        self.waiting += change
        self._changed.set()
        self._changed = asyncio.Event()


class Pages(Spider):
    """Yields the page's path, and follows its links."""

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"page": response.url.removeprefix(SITE)}
        for href in response.css("a::attr(href)").getall():
            yield Request(href)


class OnePage(Pages):
    start_urls = (f"{SITE}/a",)


def _site(*paths: str, links: dict[str, str] | None = None) -> FakeFetcher:
    """A FakeFetcher answering the pages of SITE, each linking to the page `links` gives it."""
    links = links or {}
    return FakeFetcher(
        {f"{SITE}{path}": f"<a href='{links[path]}'>next</a>" if path in links else "" for path in paths}
    )


def _paths(site: FakeFetcher) -> list[str]:
    return [request.url.removeprefix(SITE) for request in site.requests]


def _engine(
    spider: Spider,
    site: FakeFetcher,
    *,
    frontier: MemoryFrontier | None = None,
    concurrency: int = 1,
    bus: events.EventBus | None = None,
) -> tuple[Engine, HeldPausing, list[events.Event], list[Any]]:
    """An Engine with a HeldPausing, recording the lifecycle Events and the Items."""
    bus = bus or events.EventBus(timeout=5)
    recorded: list[events.Event] = []
    for event_type in (events.SpiderPaused, events.SpiderResumed, events.SpiderIdle):
        bus.on(event_type, recorded.append)
    items: list[Any] = []

    async def put_item(item: Any, _ctx: ItemContext) -> None:
        items.append(item)

    pausing = HeldPausing(spider, bus)
    engine = Engine(
        spider,
        frontier=frontier or MemoryFrontier(),
        download=site.fetch,
        handle=run_callbacks(spider),
        put_item=put_item,
        events=bus,
        concurrency=concurrency,
        pausing=pausing,
    )
    return engine, pausing, recorded, items


def _kinds(recorded: list[events.Event]) -> list[str]:
    return [type(event).__name__ for event in recorded]


async def test_a_paused_run_finishes_the_requests_in_flight_and_claims_no_more_until_resumed() -> None:
    handling, release = asyncio.Event(), asyncio.Event()

    class Gated(Pages):
        async def start(self) -> AsyncIterator[Request]:
            for path in ("/a", "/b", "/c"):
                yield Request(f"{SITE}{path}")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            handling.set()
            await release.wait()
            async for output in super().parse(response):
                yield output

    frontier = MemoryFrontier()
    site = _site("/a", "/b", "/c", "/d", links={"/a": "/d"})
    engine, pausing, recorded, items = _engine(Gated(), site, frontier=frontier, concurrency=1)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await handling.wait()
        pausing.pause()
        release.set()
        await pausing.waiting_for(2)  # the worker, and the feeding waiting for the Run to go idle

        assert (_paths(site), items) == (["/a"], [{"page": "/a"}])
        counts = await frontier.counts()
        assert (counts.ready, counts.leased, counts.done) == (3, 0, 1)  # /b, /c, and /d its Callback added

        pausing.resume()
        await run

    assert sorted(_paths(site)) == ["/a", "/b", "/c", "/d"]
    assert _kinds(recorded) == ["SpiderPaused", "SpiderResumed", "SpiderIdle"]


async def test_a_worker_waiting_for_work_claims_nothing_added_while_paused() -> None:
    handling, release = asyncio.Event(), asyncio.Event()

    class Gated(OnePage):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/a"):
                handling.set()  # the other worker waits for work meanwhile
                await release.wait()
            async for output in super().parse(response):
                yield output

    site = _site("/a", "/b", links={"/a": "/b"})
    engine, pausing, _, _ = _engine(Gated(), site, concurrency=2)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await handling.wait()
        pausing.pause()
        release.set()  # /b enters the Frontier, waking the waiting worker
        await pausing.waiting_for(3)

        assert _paths(site) == ["/a"]

        pausing.resume()
        await run

    assert _paths(site) == ["/a", "/b"]


async def test_a_pause_while_the_frontier_claims_keeps_the_worker_from_claiming_again() -> None:
    pausing: HeldPausing | None = None

    class PausedWhileClaiming(MemoryFrontier):
        """Pauses the Run while its first claim, which finds nothing, is in progress, as an awaiting Frontier may."""

        claims = 0

        async def claim(
            self,
            worker_id: str,
            limit: int,
            ttl: float,
            skip_slots: Collection[str] = frozenset(),
            *,
            rendered: bool | None = None,
        ) -> list[Lease]:
            leases = await super().claim(worker_id, limit, ttl, skip_slots, rendered=rendered)
            self.claims += 1
            if self.claims == 1:
                assert pausing is not None
                await self.add([Request(f"{SITE}/b")])
                pausing.pause()
            return leases

    class Late(Pages):
        start_urls = ()

    frontier = PausedWhileClaiming()
    site = _site("/b")
    engine, pausing, _, _ = _engine(Late(), site, frontier=frontier)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(2)

        assert (_paths(site), frontier.claims) == ([], 1)

        pausing.resume()
        await run

    assert _paths(site) == ["/b"]


async def test_a_run_paused_before_it_runs_takes_nothing_from_start_until_resumed() -> None:
    pulled: list[str] = []

    class Seeds(Pages):
        async def start(self) -> AsyncIterator[Request]:
            for path in ("/a", "/b"):
                pulled.append(path)
                yield Request(f"{SITE}{path}")

    site = _site("/a", "/b")
    engine, pausing, recorded, _ = _engine(Seeds(), site, concurrency=2)
    pausing.pause()
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(3)

        assert (pulled, _paths(site)) == ([], [])

        pausing.resume()
        await run

    assert (pulled, sorted(_paths(site))) == (["/a", "/b"], ["/a", "/b"])
    assert _kinds(recorded) == ["SpiderPaused", "SpiderResumed", "SpiderIdle"]


async def test_the_request_start_is_producing_when_paused_still_enters_the_frontier() -> None:
    producing, produce = asyncio.Event(), asyncio.Event()
    handling, release = asyncio.Event(), asyncio.Event()
    pulled: list[str] = []

    class Slow(Pages):
        async def start(self) -> AsyncIterator[Request]:
            for path in ("/a", "/b", "/c"):
                if path == "/b":
                    producing.set()
                    await produce.wait()  # as an Inline Fetch would
                pulled.append(path)
                yield Request(f"{SITE}{path}")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            handling.set()
            await release.wait()
            async for output in super().parse(response):
                yield output

    frontier = MemoryFrontier()
    site = _site("/a", "/b", "/c")
    engine, pausing, _, _ = _engine(Slow(), site, frontier=frontier)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await producing.wait()
        await handling.wait()
        pausing.pause()
        produce.set()
        release.set()
        await pausing.waiting_for(2)

        assert (pulled, _paths(site)) == (["/a", "/b"], ["/a"])
        assert (await frontier.counts()).ready == 1  # /b

        pausing.resume()
        await run

    assert _paths(site) == ["/a", "/b", "/c"]


async def test_a_paused_run_with_nothing_left_does_not_go_idle_until_resumed() -> None:
    idled: list[str] = []
    pausing: HeldPausing | None = None

    class PausesItself(OnePage):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            assert pausing is not None
            pausing.pause()  # as self.crawler.pause() would
            async for output in super().parse(response):
                yield output

        async def on_idle(self) -> AsyncIterator[Request]:
            idled.append("on_idle")
            return
            yield

    site = _site("/a")
    engine, pausing, recorded, items = _engine(PausesItself(), site)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(2)

        assert (items, idled, _kinds(recorded)) == ([{"page": "/a"}], [], ["SpiderPaused"])
        assert not run.done()

        pausing.resume()
        await run

    assert idled == ["on_idle"]
    assert _kinds(recorded) == ["SpiderPaused", "SpiderResumed", "SpiderIdle"]


async def test_a_pause_as_the_run_goes_idle_holds_on_idle_until_resumed() -> None:
    called: list[str] = []

    class More(OnePage):
        async def on_idle(self) -> AsyncIterator[Request]:
            called.append("on_idle")
            yield Request(f"{SITE}/more")

    bus = events.EventBus(timeout=5)
    site = _site("/a", "/more")
    engine, pausing, _, _ = _engine(More(), site, bus=bus)
    bus.on(events.SpiderIdle, lambda _event: None if called else pausing.pause())
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(2)

        assert (called, _paths(site)) == ([], ["/a"])

        pausing.resume()
        await run

    assert (called, _paths(site)) == (["on_idle", "on_idle"], ["/a", "/more"])


async def test_a_pause_while_the_frontier_counts_keeps_the_run_from_going_idle() -> None:
    pausing: HeldPausing | None = None

    class PausedWhileCounting(MemoryFrontier):
        """Pauses the Run while it counts, once nothing is left, as an awaiting Frontier may."""

        paused_once = False

        async def counts(self) -> FrontierCounts:
            counts = await super().counts()
            if not counts.unfinished and not self.paused_once:
                assert pausing is not None
                self.paused_once = True
                pausing.pause()
            return counts

    site = _site("/a")
    engine, pausing, recorded, _ = _engine(OnePage(), site, frontier=PausedWhileCounting())
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(2)

        assert _kinds(recorded) == ["SpiderPaused"]

        pausing.resume()
        await run

    assert _kinds(recorded) == ["SpiderPaused", "SpiderResumed", "SpiderIdle"]


async def test_a_drain_ends_a_paused_run_without_resuming_it() -> None:
    site = _site("/a")
    engine, pausing, recorded, _ = _engine(OnePage(), site, concurrency=2)
    pausing.pause()
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(3)
        engine.drain()
        await run

    assert _paths(site) == []
    assert _kinds(recorded) == ["SpiderPaused"]
    assert not pausing.paused
    pausing.resume()
    assert pausing.counts().pauses == 1


async def test_cancelling_a_paused_run_ends_its_pausing() -> None:
    site = _site("/a")
    engine, pausing, _, _ = _engine(OnePage(), site)
    pausing.pause()
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await pausing.waiting_for(2)
        run.cancel()
        await asyncio.wait([run])

    assert run.cancelled()
    assert (_paths(site), pausing.paused) == ([], False)
