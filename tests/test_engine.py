"""The Engine against an in-memory Frontier and a fake download chain, without a network."""

from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING, Any

import pytest

from pernix import IgnoreRequest, Request, Response, Retry, Spider, events
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.errors import ConnectError, RetriesExhausted
from pernix.frontiers import MemoryFrontier
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import DomainSettings, RetrySettings
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.engine import PutItem
    from pernix.frontiers import Lease
    from pernix.items import ItemContext
    from pernix.middlewares import Download
    from pernix.retry_policy import Decision, RetryPolicy

SITE = "https://site.example"


type Outcome = str | int | Exception
"""A page body for a 200 Response, a status for an empty Response, or an exception to raise."""


class FakeSite:
    """A download chain answering from a dict of pages; a missing page fails.

    A page given as a list answers with its outcomes in turn, repeating the last.
    """

    def __init__(self, pages: dict[str, Outcome | list[Outcome]]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def __call__(self, request: Request) -> Response:
        path = request.url.removeprefix(SITE)
        self.fetched.append(path)
        if path == "/ignored":
            raise IgnoreRequest("not needed")
        outcome = self.pages[path]
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, Exception):
            raise outcome
        status, body = (outcome, "") if isinstance(outcome, int) else (200, outcome)
        return Response(
            url=request.url, status=status, headers=[("Content-Type", "text/html")], body=body.encode(), request=request
        )


def _collect(items: list[Any], contexts: list[ItemContext] | None = None) -> PutItem:
    async def put(item: Any, ctx: ItemContext) -> None:
        items.append(item)
        if contexts is not None:
            contexts.append(ctx)

    return put


async def _discard(_item: Any, _ctx: ItemContext) -> None:
    pass


async def _run(
    spider: Spider,
    site: FakeSite,
    *,
    concurrency: int = 4,
    lease_ttl: float = 60.0,
    retry_policy: RetryPolicy | None = None,
    record: tuple[type[events.Event], ...] = (),
    contexts: list[ItemContext] | None = None,
) -> tuple[MemoryFrontier, list[Mapping[str, Any]], list[events.Event]]:
    """Run to the end, retrying at once by default; return the Frontier, the Items, and the recorded Events."""
    frontier = MemoryFrontier()
    items: list[Mapping[str, Any]] = []
    recorded: list[events.Event] = []
    bus = events.EventBus(timeout=5)
    for event_type in record:
        bus.on(event_type, recorded.append)
    engine = Engine(
        spider,
        frontier=frontier,
        download=site,
        handle=run_callbacks(spider),
        put_item=_collect(items, contexts),
        events=bus,
        concurrency=concurrency,
        lease_ttl=lease_ttl,
        retry_policy=retry_policy or DefaultRetryPolicy(RetrySettings(delay_scale=0)),
    )
    async with asyncio.timeout(10):
        await engine.run()
    return frontier, items, recorded


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
        spider, frontier=frontier, download=site, handle=run_callbacks(spider), put_item=_collect(items), events=bus,
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
            put_item=_discard,
            events=events.EventBus(),
            concurrency=concurrency,
            lease_ttl=lease_ttl,
        )


# Retries and failures


class Recovering(Links):
    """Follows links like Links; its errback yields an Item and a fallback Request."""

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{SITE}/a", errback=self.failed)

    async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
        yield {"failed": request.url.removeprefix(SITE), "error": type(error).__name__}
        yield Request("fallback")  # relative to the failed Request


async def test_retried_statuses_are_fetched_again_until_one_succeeds() -> None:
    site = FakeSite({"/a": [503, 429, "<p>a</p>"]})

    frontier, items, recorded = await _run(Links(), site, record=(events.RequestRetried,))

    assert site.fetched == ["/a", "/a", "/a"]
    assert items == [{"page": "a"}]
    assert [(event.attempt, event.reason) for event in recorded if isinstance(event, events.RequestRetried)] == [
        (1, "HTTP 503"),
        (2, "HTTP 429"),
    ]
    assert (await frontier.counts()).done == 1


async def test_exhausted_retries_keep_a_dead_request_and_call_the_errback() -> None:
    site = FakeSite({"/a": 503, "/fallback": "<p>fallback</p>"})

    contexts: list[ItemContext] = []
    frontier, items, recorded = await _run(Recovering(), site, record=(events.RequestFailed,), contexts=contexts)

    assert site.fetched == ["/a"] * 4 + ["/fallback"]
    assert items == [{"failed": "/a", "error": "RetriesExhausted"}, {"page": "fallback"}]
    [failed] = [event for event in recorded if isinstance(event, events.RequestFailed)]
    assert isinstance(failed.error, RetriesExhausted)
    assert failed.error.response is not None
    assert (failed.error.reason, failed.error.response.status) == ("HTTP 503", 503)
    assert (contexts[0].request.url, contexts[0].response) == (f"{SITE}/a", None)  # the errback's Item
    counts = await frontier.counts()
    assert (counts.dead, counts.done) == (1, 1)


async def test_transport_errors_are_retried_and_become_the_cause() -> None:
    site = FakeSite({"/a": ConnectError("refused", code=7), "/fallback": "<p>fallback</p>"})

    _, items, recorded = await _run(Recovering(), site, record=(events.RequestFailed,))

    assert site.fetched.count("/a") == 4
    [failed] = recorded
    assert isinstance(failed, events.RequestFailed)
    assert isinstance(failed.error, RetriesExhausted)
    assert isinstance(failed.error.__cause__, ConnectError)
    assert items[0] == {"failed": "/a", "error": "RetriesExhausted"}


async def test_errors_the_policy_keeps_fail_at_once() -> None:
    site = FakeSite({"/a": ValueError("broken fetcher"), "/fallback": "<p>fallback</p>"})

    frontier, items, _ = await _run(Recovering(), site)

    assert site.fetched == ["/a", "/fallback"]
    assert items[0] == {"failed": "/a", "error": "ValueError"}
    assert (await frontier.counts()).dead == 1


async def test_callback_errors_are_not_retried_and_keep_earlier_outputs() -> None:
    class Fragile(Recovering):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/fallback"):
                return
            yield {"page": "a"}
            raise ValueError("cannot parse")

    site = FakeSite({"/a": "<p>a</p>", "/fallback": ""})

    frontier, items, _ = await _run(Fragile(), site)

    assert site.fetched == ["/a", "/fallback"]
    assert items == [{"page": "a"}, {"failed": "/a", "error": "ValueError"}]
    counts = await frontier.counts()
    assert (counts.dead, counts.done) == (1, 1)


async def test_callbacks_can_ask_for_a_retry() -> None:
    class Patient(Links):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if "not ready" in response.text:
                raise Retry("page not ready")
            async for output in super().parse(response):
                yield output

    site = FakeSite({"/a": ["<p>not ready</p>", "<p>a</p>"]})

    frontier, items, recorded = await _run(Patient(), site, record=(events.RequestRetried,))

    assert items == [{"page": "a"}]
    assert [event.reason for event in recorded if isinstance(event, events.RequestRetried)] == ["page not ready"]
    assert (await frontier.counts()).done == 1


async def test_a_failing_retry_policy_keeps_the_outcome(caplog: pytest.LogCaptureFixture) -> None:
    class Broken:
        def decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
            raise RuntimeError("policy bug")

    site = FakeSite({"/a": 503})

    _, items, _ = await _run(Links(), site, retry_policy=Broken())

    assert site.fetched == ["/a"]
    assert items == [{"page": None}]
    assert "The retry policy failed to judge" in caplog.text


async def test_errback_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    class Clumsy(Recovering):
        async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            raise RuntimeError("errback bug")
            yield

    frontier, _, _ = await _run(Clumsy(), FakeSite({"/a": ValueError("broken")}))

    assert "Clumsy.failed failed for <Request GET https://site.example/a>" in caplog.text
    assert (await frontier.counts()).dead == 1


async def test_a_cancelled_run_returns_requests_in_flight_without_counting_an_attempt() -> None:
    started = asyncio.Event()

    class Stuck(Links):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            started.set()
            await asyncio.Event().wait()
            yield

    spider = Stuck()
    frontier = MemoryFrontier()
    retried: list[events.Event] = []
    bus = events.EventBus(timeout=5)
    bus.on(events.RequestRetried, retried.append)
    engine = Engine(
        spider,
        frontier=frontier,
        download=FakeSite({"/a": "<p>a</p>"}),
        handle=run_callbacks(spider),
        put_item=_discard,
        events=bus,
        concurrency=2,
    )
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await started.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    counts = await frontier.counts()
    assert (counts.ready, counts.leased, counts.dead) == (1, 0, 0)
    assert retried == []
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert lease.attempt == 1


async def test_transport_errors_raised_by_a_callback_are_not_retried() -> None:
    class Inline(Recovering):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/a"):
                raise ConnectError("an inline fetch failed for good", code=7)
            yield {"page": "fallback"}

    site = FakeSite({"/a": "<p>a</p>", "/fallback": ""})

    _, items, _ = await _run(Inline(), site)

    assert site.fetched == ["/a", "/fallback"]
    assert items == [{"failed": "/a", "error": "ConnectError"}, {"page": "fallback"}]


async def test_a_run_cancelled_while_settling_returns_the_request_without_counting_an_attempt() -> None:
    acking = asyncio.Event()

    class StalledAcks(MemoryFrontier):
        async def ack(self, lease: Lease) -> bool:
            acking.set()
            await asyncio.Event().wait()  # a backend that never answers
            return True

    spider = Links()
    frontier = StalledAcks()
    engine = Engine(
        spider,
        frontier=frontier,
        download=FakeSite({"/a": "<p>a</p>"}),
        handle=run_callbacks(spider),
        put_item=_discard,
        events=events.EventBus(timeout=5),
        concurrency=1,
    )
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await acking.wait()

    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    counts = await frontier.counts()
    assert (counts.ready, counts.leased) == (1, 0)
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert lease.attempt == 1


# Throttling


class Hosts(Spider):
    """Starts with six pages on each of two hosts."""

    async def start(self) -> AsyncIterator[Request]:
        for n in range(6):
            for host in ("a.example", "b.example"):
                yield Request(f"https://{host}/{n}")

    async def parse(self, response: Response) -> None:
        pass


class Probe:
    """A download chain that records how many fetches run at once, per host and in all."""

    def __init__(self, *, seconds: float = 0.02) -> None:
        self.seconds = seconds
        self.active: dict[str, int] = {}
        self.most: dict[str, int] = {}
        self.most_in_all = 0
        self.started: list[tuple[float, str]] = []

    async def __call__(self, request: Request) -> Response:
        host = request.url.split("/")[2]
        self.started.append((asyncio.get_running_loop().time(), request.url))
        self.active[host] = self.active.get(host, 0) + 1
        self.most[host] = max(self.most.get(host, 0), self.active[host])
        self.most_in_all = max(self.most_in_all, sum(self.active.values()))
        try:
            await asyncio.sleep(self.seconds)
        finally:
            self.active[host] -= 1
        return Response(url=request.url, status=200, headers=[], body=b"", request=request)


def _engine(spider: Spider, download: Download, frontier: MemoryFrontier, throttle: Throttle, **kwargs: Any) -> Engine:
    return Engine(
        spider,
        frontier=frontier,
        download=download,
        handle=run_callbacks(spider),
        put_item=_discard,
        events=events.EventBus(timeout=5),
        retry_policy=DefaultRetryPolicy(RetrySettings(delay_scale=0)),
        throttle=throttle,
        **{"concurrency": 8, **kwargs},
    )


async def test_each_domain_is_limited_while_the_others_continue() -> None:
    probe = Probe()
    frontier = MemoryFrontier()

    async with asyncio.timeout(10):
        await _engine(Hosts(), probe, frontier, Throttle(DomainSettings(concurrency=2))).run()

    assert probe.most == {"a.example": 2, "b.example": 2}
    assert probe.most_in_all == 4
    assert (await frontier.counts()).done == 12


async def test_delays_space_one_domain_without_holding_back_the_others() -> None:
    probe = Probe(seconds=0)

    async with asyncio.timeout(10):
        settings = DomainSettings(delay=0.05, delay_jitter=0.0)
        await _engine(Hosts(), probe, MemoryFrontier(), Throttle(settings)).run()

    for host in ("a.example", "b.example"):
        starts = [at for at, url in probe.started if host in url]
        assert all(later - earlier >= 0.045 for earlier, later in itertools.pairwise(starts))
    assert probe.started[-1][0] - probe.started[0][0] < 0.5


async def test_workers_skip_domains_that_must_wait() -> None:
    class OneDomainFirst(Hosts):
        async def start(self) -> AsyncIterator[Request]:
            for host in ("a.example", "b.example"):
                for n in range(6):
                    yield Request(f"https://{host}/{n}")

    probe = Probe(seconds=0)
    engine = _engine(
        OneDomainFirst(), probe, MemoryFrontier(), Throttle(DomainSettings(delay=0.1, delay_jitter=0.0)), concurrency=2
    )

    async with asyncio.timeout(10):
        await engine.run()

    first_a = min(at for at, url in probe.started if "a.example" in url)
    first_b = min(at for at, url in probe.started if "b.example" in url)
    assert first_b - first_a < 0.05, "b.example waited behind a.example's delays"


async def test_requests_claimed_for_a_cooling_domain_go_back_without_counting_an_attempt() -> None:
    class Racing(Throttle):
        def blocked(self) -> frozenset[str]:
            return frozenset()  # as if the cooldown started right after the claim

    class Recording(MemoryFrontier):
        def __init__(self) -> None:
            super().__init__()
            self.returns: list[tuple[float, bool, str]] = []

        async def nack(self, lease: Lease, *, delay: float = 0.0, count_attempt: bool = True, reason: str = "") -> bool:
            self.returns.append((round(delay, 1), count_attempt, reason))
            return await super().nack(lease, delay=delay, count_attempt=count_attempt, reason=reason)

    class One(Spider):
        start_urls = ("https://a.example/1",)

        async def parse(self, response: Response) -> None:
            pass

    throttle = Racing(DomainSettings())
    throttle.observe("a.example", Response(
        url="https://a.example/", status=429, headers=[("Retry-After", "1")], body=b"", request=Request("https://a.example/")
    ))  # fmt: skip
    frontier = Recording()
    probe = Probe(seconds=0)

    async with asyncio.timeout(10):
        await _engine(One(), probe, frontier, throttle).run()

    assert frontier.returns[0] == (1.0, False, "domain cooling down")
    assert [url for _, url in probe.started] == ["https://a.example/1"]
    assert throttle.stats.refused >= 1


async def test_requests_added_while_workers_start_waiting_are_not_missed() -> None:
    class Late(Links):
        async def start(self) -> AsyncIterator[Request]:
            await asyncio.sleep(0)  # the workers find nothing and start waiting meanwhile
            yield Request(f"{SITE}/a")

    _, items, _ = await _run(Late(), FakeSite({"/a": "<p>a</p>"}), concurrency=1)

    assert items == [{"page": "a"}]


async def test_a_lease_claimed_while_a_worker_stops_waiting_goes_back_if_cancelled() -> None:
    task: asyncio.Task[Lease | None] | None = None

    class CancelledAfterClaim(MemoryFrontier):
        async def claim(self, *args: Any, **kwargs: Any) -> list[Lease]:
            leases = await super().claim(*args, **kwargs)
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)  # lands while the waits are cleaned up
            return leases

    spider = Links()
    frontier = CancelledAfterClaim()
    await frontier.add([Request(f"{SITE}/a")])
    engine = _engine(spider, FakeSite({}), frontier, Throttle(DomainSettings()))

    task = asyncio.create_task(engine._wait_for_work())  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task

    counts = await frontier.counts()
    assert (counts.ready, counts.leased) == (1, 0)
    [lease] = await MemoryFrontier.claim(frontier, "worker-1", 1, 30.0)
    assert lease.attempt == 1


def _gated_engine(spider: Spider, site: FakeSite, frontier: MemoryFrontier, put_item: PutItem, **kwargs: Any) -> Engine:
    return Engine(
        spider,
        frontier=frontier,
        download=site,
        handle=run_callbacks(spider),
        put_item=put_item,
        events=events.EventBus(timeout=5),
        **{"concurrency": 1, **kwargs},
    )


async def test_a_drain_finishes_the_requests_in_flight_and_claims_no_more() -> None:
    handling, release = asyncio.Event(), asyncio.Event()

    class Gated(Spider):
        async def start(self) -> AsyncIterator[Request]:
            for path in ("/a", "/b", "/c"):
                yield Request(f"{SITE}{path}")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            handling.set()
            await release.wait()
            yield {"page": response.url.removeprefix(SITE)}
            yield Request(f"{SITE}/next")

    spider, frontier, items = Gated(), MemoryFrontier(), list[Any]()
    site = FakeSite({"/a": "", "/b": "", "/c": ""})
    engine = _gated_engine(spider, site, frontier, _collect(items))
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await handling.wait()
        engine.drain()
        engine.drain()
        release.set()
        await run

    assert site.fetched == ["/a"]
    assert items == [{"page": "/a"}]
    counts = await frontier.counts()
    assert (counts.ready, counts.leased, counts.done) == (3, 0, 1)  # /b, /c, and /next are kept for later


async def test_a_drain_stops_scheduling_the_requests_of_start_and_wakes_idle_workers() -> None:
    put = asyncio.Event()
    closed = asyncio.Event()

    class Endless(Links):
        async def start(self) -> AsyncIterator[Request]:
            try:
                yield Request(f"{SITE}/a")
                await asyncio.Event().wait()  # a source that never ends
                yield Request(f"{SITE}/never")
            finally:
                closed.set()

    async def put_item(_item: Any, _ctx: ItemContext) -> None:
        put.set()

    site = FakeSite({"/a": "<p>a</p>"})
    engine = _gated_engine(Endless(), site, MemoryFrontier(), put_item, concurrency=3)
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await put.wait()
        await asyncio.sleep(0.01)  # the workers are idle now
        engine.drain()
        await run

    assert closed.is_set()
    assert site.fetched == ["/a"]


async def test_an_engine_drained_before_it_runs_schedules_and_fetches_nothing() -> None:
    started: list[str] = []

    class Seeds(Links):
        async def start(self) -> AsyncIterator[Request]:
            started.append("start")
            for path in ("/a", "/b", "/c"):
                yield Request(f"{SITE}{path}")

    site = FakeSite({"/a": "<p>a</p>"})
    frontier = MemoryFrontier()
    engine = _gated_engine(Seeds(), site, frontier, _discard, concurrency=2)

    engine.drain()
    async with asyncio.timeout(5):
        await engine.run()

    assert (site.fetched, started) == ([], [])
    assert (await frontier.counts()).unfinished == 0


async def test_a_drain_when_the_run_goes_idle_skips_on_idle() -> None:
    idled: list[str] = []

    class Idle(Links):
        async def on_idle(self) -> AsyncIterator[Request]:
            idled.append("on_idle")
            yield Request(f"{SITE}/more")

    spider = Idle()
    bus = events.EventBus(timeout=5)
    engine = Engine(
        spider,
        frontier=MemoryFrontier(),
        download=FakeSite({"/a": "<p>a</p>"}),
        handle=run_callbacks(spider),
        put_item=_discard,
        events=bus,
        concurrency=1,
    )
    bus.on(events.SpiderIdle, lambda _event: engine.drain())

    async with asyncio.timeout(5):
        await engine.run()

    assert idled == []


async def test_a_cancellation_raised_by_start_is_not_taken_for_its_end() -> None:
    class Cancelled(Links):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SITE}/a")
            raise asyncio.CancelledError  # as awaiting a cancelled seed source would

    engine = _gated_engine(Cancelled(), FakeSite({"/a": "<p>a</p>"}), MemoryFrontier(), _discard)

    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await engine.run()


async def test_a_drain_from_start_still_finishes_the_requests_in_flight() -> None:
    handling, release = asyncio.Event(), asyncio.Event()
    engine: Engine | None = None

    class DrainsItself(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SITE}/a")
            await handling.wait()
            assert engine is not None
            engine.drain()  # as self.crawler.stop() would
            yield Request(f"{SITE}/b")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            handling.set()
            await release.wait()
            yield {"page": response.url.removeprefix(SITE)}

    frontier, items = MemoryFrontier(), list[Any]()
    site = FakeSite({"/a": "", "/b": ""})
    engine = _gated_engine(DrainsItself(), site, frontier, _collect(items))
    run = asyncio.create_task(engine.run())
    async with asyncio.timeout(5):
        await handling.wait()
        await asyncio.sleep(0.01)  # the feeding has ended for the Drain
        release.set()
        await run

    assert items == [{"page": "/a"}]
    counts = await frontier.counts()
    assert (counts.done, counts.ready) == (1, 0)
