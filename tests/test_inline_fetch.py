"""Inline Fetch against FakeFetchers and a fake streaming Fetcher, without a network."""

from __future__ import annotations

import asyncio
import itertools
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import IgnoreRequest, Request, Response, events
from pernix.errors import BrowserLost, ConnectError, ResponseTooLarge, RetriesExhausted, SessionError, TLSError
from pernix.fetchers import FetcherDispatch, StreamedResponse
from pernix.inline_fetch import InlineFetch, handling
from pernix.middlewares import build_download_chain
from pernix.retry_policy import Decision, DefaultRetryPolicy
from pernix.sessions import MemorySessionPool, Outcome
from pernix.sessions.session_leasing import SessionLeasing
from pernix.settings import DomainSettings, RetrySettings, SessionSettings
from pernix.stats import Stats
from pernix.testing import FakeFetcher, make_response
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping

    from pernix.middlewares import Download
    from pernix.retry_policy import RetryPolicy
    from pernix.testing import Answer

SITE = "https://site.example"


def _response(request: Request, outcome: int | bytes, **options: Any) -> Response:
    status, body = (outcome, b"") if isinstance(outcome, int) else (200, outcome)
    return Response(url=request.url, status=status, headers=[], body=body, request=request, **options)


def _site(pages: Mapping[str, list[Answer]]) -> FakeFetcher:
    """A FakeFetcher answering the pages of SITE, given by path, with their answers in turn."""
    return FakeFetcher({f"{SITE}{path}": answers for path, answers in pages.items()})


def _status(status: int) -> Answer:
    """An empty Response with a status."""
    return lambda request: make_response(request.url, status=status, request=request)


class Recording:
    """Records the Events of one type."""

    def __init__(self, bus: events.EventBus, event_type: type[events.Event]) -> None:
        self.events: list[Any] = []
        bus.on(event_type, self.events.append)


def _inline(
    download: Download,
    *,
    domain: DomainSettings | None = None,
    throttle: Throttle | None = None,
    retry_policy: RetryPolicy | None = None,
    bus: events.EventBus | None = None,
    stats: Stats | None = None,
    sessions: SessionLeasing | None = None,
) -> InlineFetch:
    return InlineFetch(
        download=download,
        throttle=throttle or Throttle(domain or DomainSettings()),
        retry_policy=retry_policy or DefaultRetryPolicy(RetrySettings(delay_scale=0)),
        events=bus or events.EventBus(timeout=5),
        stats=stats or Stats(),
        sender=sessions,
    )


def _leasing() -> SessionLeasing:
    settings = SessionSettings(enabled=True)
    return SessionLeasing(MemorySessionPool(), settings, stats=Stats())


async def test_a_fetch_returns_the_response_whatever_its_status() -> None:
    site = _site({"/a": [b"<p>a</p>"], "/missing": [_status(404)]})
    inline = _inline(site.fetch)

    found, missing = await inline.fetch(Request(f"{SITE}/a")), await inline.fetch(Request(f"{SITE}/missing"))

    assert (found.status, found.body, missing.status) == (200, b"<p>a</p>", 404)


async def test_failed_attempts_are_retried_in_place() -> None:
    site = _site({"/flaky": [_status(503), ConnectError("reset"), b"ok"]})
    bus = events.EventBus(timeout=5)
    retried = Recording(bus, events.RequestRetried)

    response = await _inline(site.fetch, bus=bus).fetch(Request(f"{SITE}/flaky"))

    assert response.body == b"ok"
    assert len(site.requests) == 3
    assert [(event.attempt, event.reason) for event in retried.events] == [
        (1, "HTTP 503"),
        (2, "ConnectError: reset"),
    ]


async def test_retries_wait_their_delay_in_place() -> None:
    site = _site({"/flaky": [_status(503), b"ok"]})

    class Briefly:
        def decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
            if isinstance(outcome, Response) and outcome.status == 503:
                return Decision.retry(0.05, "busy")
            return Decision.accept()

    started = time.monotonic()
    await _inline(site.fetch, retry_policy=Briefly()).fetch(Request(f"{SITE}/flaky"))

    assert time.monotonic() - started >= 0.05


async def test_once_the_retries_run_out_the_fetch_raises_retries_exhausted() -> None:
    site = _site({"/down": [_status(503)], "/unreachable": [ConnectError("refused")]})
    inline = _inline(site.fetch, retry_policy=DefaultRetryPolicy(RetrySettings(times=1, delay_scale=0)))

    with pytest.raises(RetriesExhausted, match="HTTP 503") as down:
        await inline.fetch(Request(f"{SITE}/down"))
    with pytest.raises(RetriesExhausted) as unreachable:
        await inline.fetch(Request(f"{SITE}/unreachable"))

    assert down.value.response is not None
    assert down.value.response.status == 503
    assert isinstance(unreachable.value.__cause__, ConnectError)
    assert unreachable.value.response is None
    assert len(site.requests) == 4


async def test_retries_under_a_budget_take_none_of_the_attempts_in_place() -> None:
    site = _site({"/a": [BrowserLost("gone"), _status(503), BrowserLost("gone"), b"ok"]})
    bus = events.EventBus(timeout=5)
    retried = Recording(bus, events.RequestRetried)
    policy = DefaultRetryPolicy(RetrySettings(times=1, browser_lost=2, delay_scale=0))

    response = await _inline(site.fetch, retry_policy=policy, bus=bus).fetch(Request(f"{SITE}/a"))

    assert response.body == b"ok"
    assert [(event.attempt, event.budget) for event in retried.events] == [
        (1, "browser_lost"),
        (1, None),
        (2, "browser_lost"),
    ]


async def test_a_spent_budget_raises_retries_exhausted_in_place() -> None:
    site = _site({"/a": [BrowserLost("gone")]})
    policy = DefaultRetryPolicy(RetrySettings(browser_lost=1, delay_scale=0))

    with pytest.raises(RetriesExhausted, match=r"browser_lost: 1 retries spent") as exhausted:
        await _inline(site.fetch, retry_policy=policy).fetch(Request(f"{SITE}/a"))

    assert isinstance(exhausted.value.__cause__, BrowserLost)
    assert len(site.requests) == 2


async def test_errors_the_policy_keeps_are_raised_as_they_are() -> None:
    site = _site({"/insecure": [TLSError("bad certificate")]})

    with pytest.raises(TLSError, match="bad certificate"):
        await _inline(site.fetch).fetch(Request(f"{SITE}/insecure"))
    assert len(site.requests) == 1


async def test_a_request_a_middleware_ignores_raises_ignore_request() -> None:
    judged: list[object] = []

    class Judging:
        def decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
            judged.append(outcome)
            return Decision.retry(0, "always")

    async def ignore(request: Request) -> Response:
        raise IgnoreRequest("not needed")

    with pytest.raises(IgnoreRequest, match="not needed"):
        await _inline(ignore, retry_policy=Judging()).fetch(Request(f"{SITE}/a"))
    assert judged == []


async def test_each_attempt_waits_for_its_slot_and_frees_it() -> None:
    running: dict[str, int] = {}
    most: dict[str, int] = {}

    async def slow(request: Request) -> Response:
        host = request.url.split("/")[2]
        running[host] = running.get(host, 0) + 1
        most[host] = max(most.get(host, 0), running[host])
        await asyncio.sleep(0.01)
        running[host] -= 1
        if request.url.endswith("/fail"):
            raise TLSError("bad certificate")
        return _response(request, b"")

    inline = _inline(slow, domain=DomainSettings(concurrency=1))
    urls = [f"{SITE}/{n}" for n in range(3)] + ["https://other.example/0", "https://other.example/1"]
    with pytest.raises(TLSError):
        await inline.fetch(Request(f"{SITE}/fail"))

    async with asyncio.timeout(5):
        await asyncio.gather(*(inline.fetch(Request(url)) for url in urls))

    assert most == {"site.example": 1, "other.example": 1}


async def test_a_cooling_slot_is_waited_out_in_place() -> None:
    site = _site({"/limited": [b"ok"]})
    throttle = Throttle(DomainSettings(cooldown_max=0.05))
    inline = _inline(site.fetch, throttle=throttle)
    request = Request(f"{SITE}/limited")
    throttle.observe("site.example", _response(request, 429).replace(headers=[("Retry-After", "60")]))

    started = time.monotonic()
    response = await inline.fetch(request)

    assert response.status == 200
    assert time.monotonic() - started >= 0.04
    assert throttle.stats.refused >= 1


async def test_a_request_continues_the_session_of_the_request_being_handled() -> None:
    site = _site({"/a": [b""]})
    inline = _inline(site.fetch, sessions=_leasing())

    with handling(Request(f"{SITE}/page", session="first")):
        await inline.fetch(Request(f"{SITE}/a"))
        await inline.fetch(Request(f"{SITE}/a", session="own"))
    await inline.fetch(Request(f"{SITE}/a"))

    continued, own, leased = (request.session for request in site.requests)
    assert (continued, own) == ("first", "own")
    assert leased is not None  # one the pool gives


async def test_naming_a_session_without_a_session_pool_fails() -> None:
    site = _site({"/a": [b""]})
    inline = _inline(site.fetch)

    with pytest.raises(SessionError, match="no Session Pool is enabled"):
        await inline.fetch(Request(f"{SITE}/a", session="alice"))
    with handling(Request(f"{SITE}/page")):
        response = await inline.fetch(Request(f"{SITE}/a"))

    assert response.request.session is None
    assert len(site.requests) == 1


class Files:
    """A Fetcher of fixed bodies that can stream them, tracking its open transfers."""

    schemes: ClassVar[frozenset[str]] = frozenset({"https"})

    def __init__(self, outcomes: list[int | bytes]) -> None:
        self.outcomes = outcomes
        self.opened = 0
        self.open = 0
        self.limits: list[int | None] = []

    def _next(self) -> int | bytes:
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]

    async def fetch(self, request: Request) -> Response:
        return _response(request, self._next())

    @asynccontextmanager
    async def stream(self, request: Request, *, max_bytes: int | None = None) -> AsyncGenerator[StreamedResponse]:
        outcome = self._next()
        body = b"" if isinstance(outcome, int) else outcome
        self.opened += 1
        self.open += 1
        self.limits.append(max_bytes)
        try:
            response = _response(request, outcome if isinstance(outcome, int) else 200, streamed=True)
            yield StreamedResponse(response, lambda: _halves(body), max_bytes=max_bytes or None)
        finally:
            self.open -= 1


async def _halves(body: bytes) -> AsyncIterator[bytes]:
    middle = len(body) // 2
    yield body[:middle]
    yield body[middle:]


type Layer = Callable[[Request, Download], Awaitable[Response]]


def _chain(fetcher: object, *layers: Layer) -> Download:
    return build_download_chain(list(layers), FetcherDispatch({"files": fetcher}))


async def _read(streamed: StreamedResponse) -> bytes:
    return b"".join([chunk async for chunk in streamed.iter_bytes()])


async def test_a_streamed_fetch_passes_the_middleware_a_response_without_its_body() -> None:
    files = Files([b"0123456789"])
    seen: list[tuple[bool, bytes]] = []

    async def record(request: Request, call_next: Download) -> Response:
        response = await call_next(request)
        seen.append((response.streamed, response.body))
        return response.replace(headers=[("X-Seen", "yes")])

    stats = Stats()
    inline = _inline(_chain(files, record), stats=stats)
    async with inline.stream(Request(f"{SITE}/file"), max_bytes=100) as streamed:
        body = await _read(streamed)

    assert seen == [(True, b"")]
    assert body == b"0123456789"
    assert (streamed.response.fetcher, streamed.response.headers.get("X-Seen")) == ("files", "yes")
    assert (files.open, files.limits) == (0, [100])
    assert stats.snapshot()["downloads.bytes"] == 10


async def test_a_middleware_may_answer_a_streamed_fetch_with_a_whole_response() -> None:
    files = Files([b"fetched"])

    async def cache(request: Request, call_next: Download) -> Response:
        return _response(request, b"cached")

    async with _inline(_chain(files, cache)).stream(Request(f"{SITE}/file")) as streamed:
        body = await _read(streamed)

    assert body == b"cached"
    assert (streamed.response.streamed, streamed.response.body) == (True, b"")
    assert files.opened == 0


async def test_a_middleware_fetching_twice_reads_the_second_transfer_and_closes_the_first() -> None:
    files = Files([b"first", b"second"])
    open_while_fetching_again: list[int] = []

    async def twice(request: Request, call_next: Download) -> Response:
        await call_next(request)
        open_while_fetching_again.append(files.open)
        return await call_next(request)

    async with _inline(_chain(files, twice)).stream(Request(f"{SITE}/file")) as streamed:
        assert files.open == 1
        body = await _read(streamed)

    assert body == b"second"
    assert open_while_fetching_again == [1]
    assert (files.opened, files.open) == (2, 0)


async def test_a_fetcher_without_stream_is_read_whole() -> None:
    class Whole:
        schemes: ClassVar[frozenset[str]] = frozenset({"https"})

        async def fetch(self, request: Request) -> Response:
            return _response(request, b"whole body")

    inline = _inline(_chain(Whole()))
    async with inline.stream(Request(f"{SITE}/file")) as streamed:
        assert await _read(streamed) == b"whole body"
    async with inline.stream(Request(f"{SITE}/file"), max_bytes=4) as streamed:
        with pytest.raises(ResponseTooLarge, match="exceeds 4 bytes"):
            await _read(streamed)


async def test_a_stream_holds_its_slot_until_it_is_closed() -> None:
    files = Files([b"data"])
    inline = _inline(_chain(files), domain=DomainSettings(concurrency=1))
    order: list[str] = []

    async def other() -> None:
        await inline.fetch(Request(f"{SITE}/other"))
        order.append("other fetched")

    async with asyncio.timeout(5):
        async with inline.stream(Request(f"{SITE}/file")) as streamed:
            waiting = asyncio.create_task(other())
            await asyncio.sleep(0.02)
            await _read(streamed)
            order.append("stream closing")
        await waiting

    assert order == ["stream closing", "other fetched"]


async def test_a_streamed_fetch_is_retried_until_its_headers_are_accepted() -> None:
    files = Files([503, b"ok"])
    bus = events.EventBus(timeout=5)
    retried = Recording(bus, events.RequestRetried)

    async with _inline(_chain(files), bus=bus).stream(Request(f"{SITE}/file")) as streamed:
        body = await _read(streamed)

    assert body == b"ok"
    assert [event.reason for event in retried.events] == ["HTTP 503"]
    assert (files.opened, files.open) == (2, 0)


def _authorizing(files: Files, nested: list[tuple[bool, bytes]]) -> InlineFetch:
    """An Inline Fetch with one slot per domain, whose Middleware fetches a token from the same domain first."""
    inline: InlineFetch | None = None

    async def authorize(request: Request, call_next: Download) -> Response:
        if request.url.endswith("/token"):
            return await call_next(request)
        assert inline is not None
        token = await inline.fetch(Request(f"{SITE}/token"))
        nested.append((token.streamed, token.body))
        return await call_next(request.replace(headers=[("Authorization", token.text)]))

    inline = _inline(_chain(files, authorize), domain=DomainSettings(concurrency=1))
    return inline


async def test_a_fetch_a_middleware_nests_shares_the_slot_of_the_outer_fetch() -> None:
    files = Files([b"secret"])
    nested: list[tuple[bool, bytes]] = []
    inline = _authorizing(files, nested)

    async with asyncio.timeout(2):
        response = await inline.fetch(Request(f"{SITE}/data"))

    assert response.request.headers.get("Authorization") == "secret"
    assert nested == [(False, b"secret")]


async def test_fetches_nested_in_a_streamed_fetch_share_its_slot_and_read_whole_responses() -> None:
    files = Files([b"data"])
    nested: list[tuple[bool, bytes]] = []
    inline = _authorizing(files, nested)

    async with asyncio.timeout(2), inline.stream(Request(f"{SITE}/file")) as streamed:
        assert await _read(streamed) == b"data"

    assert nested == [(False, b"data")]
    assert files.opened == 1


async def test_fetches_in_the_block_of_a_stream_share_its_slot() -> None:
    files = Files([b"data"])
    inline = _inline(_chain(files), domain=DomainSettings(concurrency=1))

    async with asyncio.timeout(2), inline.stream(Request(f"{SITE}/file")) as streamed:
        page = await inline.fetch(Request(f"{SITE}/page"))
        body = await _read(streamed)

    assert (page.body, body) == (b"data", b"data")


async def test_fetches_sharing_a_slot_still_wait_for_the_delay() -> None:
    files = Files([b"secret"])
    started: dict[str, float] = {}
    inline: InlineFetch | None = None

    async def authorize(request: Request, call_next: Download) -> Response:
        started[request.url.removeprefix(SITE)] = time.monotonic()
        if request.url.endswith("/token"):
            return await call_next(request)
        assert inline is not None
        await inline.fetch(Request(f"{SITE}/token"))
        return await call_next(request)

    inline = _inline(_chain(files, authorize), domain=DomainSettings(concurrency=1, delay=0.05, delay_jitter=0.0))
    async with asyncio.timeout(2):
        await inline.fetch(Request(f"{SITE}/data"))

    assert started["/token"] - started["/data"] >= 0.045


async def test_fetches_sharing_a_slot_wait_out_its_cooldown() -> None:
    files = Files([b"data"])
    throttle = Throttle(DomainSettings(concurrency=1, cooldown_max=0.05))
    inline = _inline(_chain(files), throttle=throttle)

    async with asyncio.timeout(2), inline.stream(Request(f"{SITE}/file")) as streamed:
        throttle.observe(
            "site.example", _response(streamed.response.request, 429).replace(headers=[("Retry-After", "60")])
        )
        started = time.monotonic()
        await inline.fetch(Request(f"{SITE}/page"))
        waited = time.monotonic() - started
        await _read(streamed)

    assert waited >= 0.04
    assert throttle.stats.refused == 1


async def test_tasks_started_inside_a_fetch_wait_for_slots_of_their_own() -> None:
    files = Files([b"data"])
    running = most = 0

    async def track(request: Request, call_next: Download) -> Response:
        nonlocal running, most
        if request.url.endswith("/file"):
            return await call_next(request)
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return await call_next(request)

    inline = _inline(_chain(files, track), domain=DomainSettings(concurrency=2))
    async with asyncio.timeout(5), inline.stream(Request(f"{SITE}/file")) as streamed:
        await asyncio.gather(*(inline.fetch(Request(f"{SITE}/{n}")) for n in range(3)))
        await _read(streamed)

    assert most == 1  # the stream holds one slot of two; the tasks share the other


async def test_the_domain_paces_fetches_once_the_session_they_waited_for_frees_up() -> None:
    starts: list[float] = []
    site = _site({"/a": [b""]})

    async def timed(request: Request) -> Response:
        starts.append(time.monotonic())
        return await site.fetch(request)

    pool = MemorySessionPool(size=1)
    leasing = SessionLeasing(pool, SessionSettings(enabled=True), stats=Stats())
    inline = _inline(timed, domain=DomainSettings(delay=0.1, delay_jitter=0.0), sessions=leasing)
    busy = await pool.acquire()  # the only Session, held for a while
    assert busy is not None

    fetches = [asyncio.create_task(inline.fetch(Request(f"{SITE}/a?n={number}"))) for number in range(3)]
    await asyncio.sleep(0.3)
    await pool.release(busy, Outcome("ok"))
    await asyncio.gather(*fetches)

    assert min(later - earlier for earlier, later in itertools.pairwise(starts)) >= 0.09


async def test_a_fetch_waiting_for_a_place_holds_no_session_a_nested_fetch_needs() -> None:
    site = _site({"/outer": [b"outer"], "/inner": [b"inner"], "/other": [b"other"]})
    downloading = asyncio.Event()
    inline: InlineFetch

    async def download(request: Request) -> Response:
        if request.url.endswith("/outer"):
            downloading.set()
            await asyncio.sleep(0.05)  # the other fetch arrives meanwhile, and waits for the place
            await inline.fetch(Request(f"{SITE}/inner", session="b"))  # nested, in the same domain
        return await site.fetch(request)

    pool = MemorySessionPool(size=2)
    leasing = SessionLeasing(pool, SessionSettings(enabled=True), stats=Stats())
    inline = _inline(download, domain=DomainSettings(concurrency=1), sessions=leasing)

    outer = asyncio.create_task(inline.fetch(Request(f"{SITE}/outer", session="a")))
    await downloading.wait()
    other = asyncio.create_task(inline.fetch(Request(f"{SITE}/other", session="b")))

    async with asyncio.timeout(5):
        responses = await asyncio.gather(outer, other)

    assert [response.body for response in responses] == [b"outer", b"other"]


async def test_a_fetch_waiting_for_a_session_gives_up_its_place_to_a_nested_fetch_of_another_domain() -> None:
    other_site = "https://other.example"
    site = FakeFetcher({f"{SITE}/outer": b"outer", f"{other_site}/nested": b"nested", f"{other_site}/rival": b"rival"})
    downloading = asyncio.Event()
    inline: InlineFetch

    async def download(request: Request) -> Response:
        if request.url.endswith("/outer"):
            downloading.set()
            await asyncio.sleep(0.05)  # the rival takes the other domain's place meanwhile, and waits for "s"
            await inline.fetch(Request(f"{other_site}/nested"))  # continues "s", in the other domain
        return await site.fetch(request)

    leasing = SessionLeasing(MemorySessionPool(size=1), SessionSettings(enabled=True), stats=Stats())
    inline = _inline(download, domain=DomainSettings(concurrency=1), sessions=leasing)

    outer = asyncio.create_task(inline.fetch(Request(f"{SITE}/outer", session="s")))
    await downloading.wait()
    rival = asyncio.create_task(inline.fetch(Request(f"{other_site}/rival", session="s")))

    async with asyncio.timeout(5):
        responses = await asyncio.gather(outer, rival)

    assert [response.body for response in responses] == [b"outer", b"rival"]
    assert [request.url.rsplit("/", 1)[-1] for request in site.requests] == ["nested", "outer", "rival"]
