"""The time limit of Callbacks and errbacks, `callbacks.timeout`, in whole crawls."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Pipeline, Request, Response, Spider
from pernix.crawler import Crawler
from pernix.errors import CallbackTimeout, ConnectError, SettingsError
from pernix.testing import FakeFetcher, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.items import ItemContext
    from pernix.testing import CrawlReport

SHOP = "https://shop.example"


def _shop(*paths: str) -> FakeFetcher:
    return FakeFetcher({f"{SHOP}{path}": f"<h1>{path}</h1>" for path in paths})


async def _crawl(spider: type[Spider], fetcher: FakeFetcher, settings: Mapping[str, Any] | None = None) -> CrawlReport:
    async with asyncio.timeout(30):  # a Callback the limit misses would wait forever
        return await crawl(spider, fetcher=fetcher, settings=settings)


async def test_a_callback_awaiting_past_the_limit_is_cancelled_and_its_errback_gets_the_error() -> None:
    errors: list[Exception] = []

    class Stuck(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SHOP}/stuck", errback=self.failed)
            yield Request(f"{SHOP}/next")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"page": response.css("h1::text").get()}
            if response.url.endswith("/stuck"):
                await asyncio.Event().wait()  # never set, as a database that never answers
                yield {"never": True}

        async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            errors.append(error)
            yield {"failed": request.url}

    report = await _crawl(Stuck, _shop("/stuck", "/next"), {"callbacks.timeout": 0.5})

    assert report.reason == "finished"
    assert sorted(report.items, key=str) == sorted(
        [{"page": "/stuck"}, {"page": "/next"}, {"failed": f"{SHOP}/stuck"}], key=str
    )
    [error] = errors
    assert isinstance(error, CallbackTimeout)
    assert isinstance(error, TimeoutError)
    assert error.timeout == 0.5
    assert str(error).endswith("Stuck.parse ran longer than callbacks.timeout (0.5 s)")
    assert isinstance(error.__cause__, TimeoutError)  # its traceback shows where the Callback waited
    assert (report.stats["callbacks.timeouts"], report.stats["callbacks.errback_timeouts"]) == (1, 0)
    assert report.stats["requests.failed_by_error.CallbackTimeout"] == 1
    assert (report.stats["frontier.dead"], report.stats["frontier.done"]) == (1, 1)


async def test_with_no_limit_a_long_callback_runs_to_its_end() -> None:
    class Slow(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            await asyncio.sleep(0.2)
            yield {"done": True}

    report = await _crawl(Slow, _shop("/"))

    assert report.items == [{"done": True}]
    assert not {"callbacks.timeouts", "callbacks.errback_timeouts"} & report.stats.keys()


class SlowPipeline(Pipeline):
    """Holds each Item longer than the Callbacks may take in all."""

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        await asyncio.sleep(0.4)
        return item


async def test_the_time_outputs_wait_to_be_handed_on_does_not_count() -> None:
    class Many(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(4):
                yield {"n": n}  # each waits for room in the Item queue, which the Pipeline frees slowly

    settings = {"callbacks.timeout": 0.3, "pipelines": {SlowPipeline: 100}, "items.queue_size": 1}
    report = await _crawl(Many, _shop("/"), settings)

    assert report.items == [{"n": n} for n in range(4)]
    assert report.stats["callbacks.timeouts"] == 0


async def test_an_output_given_after_the_time_ran_out_without_awaiting_is_not_taken() -> None:
    class Busy(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(3):
                time.sleep(0.3)  # blocks the event loop, so nothing can cancel it  # noqa: ASYNC251
                yield {"n": n}

    report = await _crawl(Busy, _shop("/"), {"callbacks.timeout": 0.5})

    assert report.items == [{"n": 0}]  # the second came after the time ran out
    assert report.stats["callbacks.timeouts"] == 1
    assert report.stats["requests.failed_by_error.CallbackTimeout"] == 1


async def test_a_callback_ending_after_the_time_ran_out_without_awaiting_fails_all_the_same() -> None:
    class Late(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"n": 0}
            time.sleep(0.6)  # noqa: ASYNC251

    report = await _crawl(Late, _shop("/"), {"callbacks.timeout": 0.5})

    assert report.items == [{"n": 0}]
    assert report.stats["callbacks.timeouts"] == 1
    assert (report.stats["frontier.dead"], report.stats["frontier.done"]) == (1, 0)


async def test_a_callback_stopped_after_its_time_ran_out_fails_with_its_cleanup_and_the_run_goes_on() -> None:
    errors: list[Exception] = []

    class Untidy(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SHOP}/", errback=self.failed)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            try:
                time.sleep(0.6)  # noqa: ASYNC251
                yield {"late": True}
            finally:
                raise ValueError("cleanup failed")

        async def failed(self, request: Request, error: Exception) -> None:
            errors.append(error)

    report = await _crawl(Untidy, _shop("/"), {"callbacks.timeout": 0.5})

    assert report.reason == "finished"
    assert report.items == []
    [error] = errors
    assert isinstance(error, CallbackTimeout)
    assert isinstance(error.__cause__, ValueError)
    assert report.stats["callbacks.timeouts"] == 1


async def test_a_callback_that_swallows_its_cancellation_is_stopped_all_the_same() -> None:
    class Stubborn(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
            yield {"went on": True}

    report = await _crawl(Stubborn, _shop("/"), {"callbacks.timeout": 0.05})

    assert report.items == []
    assert report.stats["callbacks.timeouts"] == 1
    assert report.stats["requests.failed_by_error.CallbackTimeout"] == 1


async def test_an_errback_running_past_the_limit_is_logged_and_keeps_its_outputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing(Spider):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SHOP}/down", errback=self.failed)

        async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            yield {"failed": request.url}
            await asyncio.Event().wait()
            yield {"never": True}

    fetcher = FakeFetcher({f"{SHOP}/down": ConnectError("refused")})
    settings = {"callbacks.timeout": 0.5, "retry.times": 0}
    report = await _crawl(Failing, fetcher, settings)

    assert report.reason == "finished"
    assert report.items == [{"failed": f"{SHOP}/down"}]
    assert (report.stats["callbacks.timeouts"], report.stats["callbacks.errback_timeouts"]) == (0, 1)
    [record] = [record for record in caplog.records if record.getMessage().startswith("Errback ")]
    assert record.levelno == logging.ERROR
    assert "CallbackTimeout: " in str(record.exc_text)
    assert "Failing.failed ran longer than callbacks.timeout (0.5 s)" in str(record.exc_text)


async def test_a_force_stop_still_cancels_a_callback_under_a_limit() -> None:
    class Stalling(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            asyncio.get_running_loop().call_soon(self.crawler.stop, "force")
            await asyncio.Event().wait()
            yield

    crawler = Crawler.from_spider(Stalling, settings={"callbacks.timeout": 30, "fetchers": {"http": _shop("/")}})
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    stats = crawler.stats.snapshot()
    assert result.reason == "forced"
    assert stats["callbacks.timeouts"] == 0
    assert (stats["frontier.dead"], stats["frontier.ready"]) == (0, 1)  # returned to the Frontier


@pytest.mark.parametrize("timeout", [0, -1])
def test_the_limit_must_be_positive(timeout: float) -> None:
    with pytest.raises(SettingsError, match=r"callbacks\.timeout"):
        Crawler.from_spider(Spider, settings={"callbacks.timeout": timeout})
