"""Pages kept for Callbacks, with stand-in pages: where they are kept, when they close, and how their failures count."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from pernix import Request, Response, Spider, events
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.errors import BrowserLost, PageCrashed
from pernix.fetchers.kept_pages import KeptPage, keeping_pages, page_keeping, sharing_a_kept_slot, without_keeping
from pernix.frontiers import MemoryFrontier
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import RetrySettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from patchright.async_api import Page

SITE = "https://site.example"


class Kept:
    """A stand-in for a kept page: it records whether it was closed, and may have gone away."""

    def __init__(self, broken: Exception | None = None) -> None:
        self.closed = 0
        self.broken = broken

    def page(self) -> KeptPage:
        return KeptPage(cast("Page", self), close=self.close, broken=lambda: self.broken)

    def close(self) -> None:
        self.closed += 1


async def test_a_page_is_kept_until_the_scope_ends() -> None:
    kept = Kept()

    async with keeping_pages() as keeping:
        assert page_keeping() is keeping
        assert not sharing_a_kept_slot()
        keeping.keep(kept.page())
        assert sharing_a_kept_slot()
        assert kept.closed == 0

    assert kept.closed == 1
    assert page_keeping() is None


async def test_a_cancelled_scope_still_closes_its_pages() -> None:
    kept = Kept()
    inside = asyncio.Event()

    async def keep() -> None:
        async with keeping_pages() as keeping:
            keeping.keep(kept.page())
            inside.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(keep())
    await inside.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert kept.closed == 1


async def test_fetches_refused_keeping_still_share_the_slot_of_a_page_kept() -> None:
    async with keeping_pages() as keeping:
        keeping.keep(Kept().page())
        with without_keeping():
            assert page_keeping() is None
            assert sharing_a_kept_slot()

        async def in_a_subtask() -> bool:
            return sharing_a_kept_slot()

        assert await asyncio.create_task(in_a_subtask())


async def test_no_page_is_kept_outside_a_scope() -> None:
    assert page_keeping() is None
    assert not sharing_a_kept_slot()


def test_closing_a_kept_page_twice_closes_it_once() -> None:
    kept = Kept()
    page = kept.page()

    page.close()
    page.close()

    assert kept.closed == 1


# A Callback whose kept page went away


class Clicking(Spider):
    """Keeps the page of its one Request, and fails on it while it is broken."""

    def __init__(self, kept: list[Kept]) -> None:
        self.kept = kept

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{SITE}/page")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        if self.kept[-1].broken is not None:
            raise RuntimeError("Page.click: Target page, context or browser has been closed")
        yield {"attempts": len(self.kept)}


async def _run(spider: Clicking, broken: list[Exception | None], **retry: Any) -> tuple[list[events.Event], list[Any]]:
    """Run a Spider whose fetches keep a stand-in page each, broken as `broken` says in turn; return Events and Items."""
    recorded: list[events.Event] = []
    bus = events.EventBus(timeout=5)
    for event_type in (events.RequestRetried, events.RequestFailed):
        bus.on(event_type, recorded.append)

    async def download(request: Request) -> Response:
        kept = Kept(broken[len(spider.kept)])
        spider.kept.append(kept)
        keeping = page_keeping()
        assert keeping is not None
        keeping.keep(kept.page())
        return Response(url=request.url, status=200, headers=[], body=b"", request=request)

    items: list[Any] = []

    async def put(item: Any, _context: object) -> None:
        items.append(item)

    engine = Engine(
        spider,
        frontier=MemoryFrontier(),
        download=download,
        handle=run_callbacks(spider),
        put_item=put,
        events=bus,
        concurrency=1,
        retry_policy=DefaultRetryPolicy(RetrySettings(delay_scale=0, **retry)),
    )
    async with asyncio.timeout(10):
        await engine.run()
    return recorded, items


async def test_a_callback_failing_as_its_page_crashed_is_retried_as_a_crash() -> None:
    spider = Clicking([])

    recorded, items = await _run(spider, [PageCrashed("the page crashed"), None])

    [retried] = [event for event in recorded if isinstance(event, events.RequestRetried)]
    assert (retried.attempt, retried.budget) == (1, None)
    assert retried.reason == "PageCrashed: the page crashed"
    assert [kept.closed for kept in spider.kept] == [1, 1]
    assert items == [{"attempts": 2}]


async def test_a_callback_failing_as_its_browser_was_lost_is_retried_under_the_budget() -> None:
    spider = Clicking([])

    recorded, _ = await _run(spider, [BrowserLost("the browser disconnected"), None], times=0)

    [retried] = [event for event in recorded if isinstance(event, events.RequestRetried)]
    assert retried.budget == "browser_lost"


async def test_the_callbacks_own_error_becomes_the_cause_once_retries_run_out() -> None:
    spider = Clicking([])

    recorded, _ = await _run(spider, [PageCrashed("the page crashed")] * 2, times=1)

    [failed] = [event for event in recorded if isinstance(event, events.RequestFailed)]
    crashed = failed.error.__cause__
    assert isinstance(crashed, PageCrashed)
    assert isinstance(crashed.__cause__, RuntimeError)
    assert "Target page" in str(crashed.__cause__)


async def test_a_callback_whose_page_did_not_go_away_fails_as_callbacks_do() -> None:
    class Fragile(Clicking):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            raise ValueError("cannot parse")
            yield

    spider = Fragile([])

    recorded, _ = await _run(spider, [None])

    assert not [event for event in recorded if isinstance(event, events.RequestRetried)]
    [failed] = [event for event in recorded if isinstance(event, events.RequestFailed)]
    assert isinstance(failed.error, ValueError)
    assert spider.kept[0].closed == 1
