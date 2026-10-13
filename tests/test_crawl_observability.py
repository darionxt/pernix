"""Logs and stats of whole crawls against the loopback server."""

from __future__ import annotations

import asyncio
import io
import logging
import socket
import sys
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from loopback import Reply
from pernix import Extension, Request, Response, Spider, events
from pernix.crawler import Crawler
from pernix.errors import ComponentError
from pernix.settings import Secret, Section

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from loopback import LoopbackServer
    from pernix.stats import Stats  # injected by its name, so a type-only import suffices


def _json_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [orjson.loads(line) for line in stream.getvalue().splitlines()]


async def _crawl(spider: type[Spider], settings: Mapping[str, Any] | None = None) -> Crawler:
    crawler = Crawler.from_spider(spider, settings=settings)
    async with asyncio.timeout(10):
        await crawler.crawl()
    return crawler


async def test_json_lines_carry_the_run_id_and_the_request_ids(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.route("/", "<a href='/a'>a</a><a href='/b'>b</a>")
    server.route("/a", "a")
    server.route("/b", "b")
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    class Pages(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.logger.info("parsed %s", response.url)
            for href in response.css("a::attr(href)").getall():
                yield Request(href)

    crawler = await _crawl(Pages, {"log.output": "stderr", "log.format": "json"})

    lines = _json_lines(stream)
    parsed = [line for line in lines if line["logger"] == "pernix.spiders.pages"]
    assert len(parsed) == 3
    assert {(line["run_id"], line["spider"]) for line in lines} == {(crawler.run_id, "pages")}
    assert len({line["request_id"] for line in parsed}) == 3
    assert None not in {line["request_id"] for line in parsed}
    [summary] = [line for line in lines if line.get("final") is True]
    assert summary["request_id"] is None
    assert summary["stats"]["run.id"] == crawler.run_id
    assert summary["stats"]["downloads.responses"] == 3


async def test_core_stats_count_requests_responses_and_items(server: LoopbackServer) -> None:
    replies = [Reply(status=503), Reply(body="ok")]
    server.route("/flaky", handler=lambda _request: replies.pop(0))
    server.route("/", "<a href='/a'>a</a><a href='/a'>again</a><a href='/missing'>404</a><a href='/flaky'>f</a>")
    server.route("/a", "a")
    server.route("/missing", "gone", status=404)

    class Site(Spider):
        start_urls = (server.url("/"),)
        allowed_domains = ("127.0.0.1",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"status": response.status}
            for href in response.css("a::attr(href)").getall():
                yield Request(href)
            yield Request("https://elsewhere.example/")

    crawler = await _crawl(Site, {"retry.delay_scale": 0})

    stats = crawler.stats.snapshot()
    assert {
        key: stats[key] for key in stats if key.startswith(("requests.", "downloads.", "responses.", "items."))
    } == {
        "requests.enqueued": 4,
        "requests.dropped": 5,  # the second /a, and elsewhere.example from each of the 4 parsed Responses
        "requests.dropped_by_reason.duplicate": 1,
        "requests.dropped_by_reason.offsite": 4,
        "requests.retried": 1,
        "requests.ignored": 0,
        "requests.failed": 0,
        "downloads.responses": 5,
        "downloads.by_status.200": 3,
        "downloads.by_status.404": 1,
        "downloads.by_status.503": 1,
        "downloads.bytes": len(
            b"<a href='/a'>a</a><a href='/a'>again</a><a href='/missing'>404</a><a href='/flaky'>f</a>"
        )
        + len(b"a")
        + len(b"gone")
        + len(b"ok"),
        "downloads.errors": 0,
        "responses.received": 4,
        "items.scraped": 4,
        "items.dropped": 0,
        "items.errors": 0,
    }
    assert (stats["frontier.done"], stats["frontier.ready"], stats["frontier.dead"]) == (4, 0, 0)
    assert (stats["run.id"], stats["run.spider"], stats["run.reason"]) == (crawler.run_id, "site", "finished")
    assert {"run.started_at", "run.finished_at", "run.elapsed", "loop.lag_avg", "loop.lag_max"} <= stats.keys()
    assert {"throttle.waits", "throttle.wait_seconds", "throttle.cooldowns", "throttle.refused"} <= stats.keys()
    assert {"log.warning", "log.error", "log.critical", "log.dropped"} <= stats.keys()


async def test_failures_are_counted_by_error_type(server: LoopbackServer) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    server.route("/broken", "ok")

    class Failing(Spider):
        start_urls = (f"http://127.0.0.1:{closed_port}/", server.url("/broken"))

        async def parse(self, response: Response) -> None:
            raise ValueError("cannot parse")

    crawler = await _crawl(Failing, {"retry.times": 0})

    stats = crawler.stats.snapshot()
    assert stats["downloads.errors"] == 1
    assert stats["downloads.errors_by_type.ConnectError"] == 1
    assert stats["requests.failed"] == 2
    assert stats["requests.failed_by_error.RetriesExhausted"] == 1
    assert stats["requests.failed_by_error.ValueError"] == 1
    assert int(stats["log.error"]) >= 2
    assert stats["frontier.dead"] == 2


class SinkSettings(Section):
    __prefix__ = "test_sink"
    label: str = "sink"


class RecordingSink:
    instances: list[RecordingSink] = []  # noqa: RUF012

    def __init__(self, stats: Stats, settings: SinkSettings) -> None:
        self.stats = stats
        self.label = settings.label
        self.calls: list[str] = []
        self.snapshots: list[events.StatsSnapshot] = []
        RecordingSink.instances.append(self)

    async def open(self) -> None:
        self.calls.append("open")

    async def push(self, snapshot: events.StatsSnapshot) -> None:
        self.calls.append("final" if snapshot.final else "periodic")
        self.snapshots.append(snapshot)

    async def close(self) -> None:
        self.calls.append("close")


async def test_stats_sinks_receive_periodic_and_final_snapshots(server: LoopbackServer) -> None:
    server.route("/", "ok")
    RecordingSink.instances.clear()

    class Slow(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> None:
            await asyncio.sleep(0.12)
            self.stats.inc("slow.pages")

    crawler = await _crawl(
        Slow, {"stats.interval": 0.03, "stats.sinks": {RecordingSink: 1}, "test_sink.label": "platform"}
    )

    [sink] = RecordingSink.instances
    assert sink.stats is crawler.stats
    assert sink.label == "platform"
    assert sink.calls[0] == "open"
    assert sink.calls[-2:] == ["final", "close"]
    assert sink.calls.count("periodic") >= 2
    assert sink.snapshots[-1].stats["slow.pages"] == 1
    assert sink.snapshots[-1].stats == crawler.stats.snapshot()


async def test_stats_sinks_must_have_push() -> None:
    class Idle(Spider):
        pass

    class Mute:
        pass

    with pytest.raises(ComponentError, match=r"stats\.sinks: .* must have push\(snapshot\)"):
        await Idle.crawl(settings={"stats.sinks": {Mute: 1}})


async def test_the_run_id_comes_from_the_settings_or_is_generated() -> None:
    class Idle(Spider):
        pass

    given = await _crawl(Idle, {"run_id": "nightly-42"})
    generated = Crawler.from_spider(Idle)

    assert given.run_id == "nightly-42"
    assert given.stats.get("run.id") == "nightly-42"
    assert len(generated.run_id) == 32
    assert int(generated.run_id, 16) >= 0
    assert generated.run_id != Crawler.from_spider(Idle).run_id


class TokenSettings(Section):
    __prefix__ = "tokens"
    api: Secret = Secret("")


class UsesToken(Extension):
    def __init__(self, tokens: TokenSettings) -> None:
        self.tokens = tokens


async def test_secret_settings_are_masked_in_the_log_output(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.route("/", "ok")
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    class Careless(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> None:
            token = self.crawler.settings.section(TokenSettings).api.reveal()
            self.logger.warning("calling the API with %s", token)

    await _crawl(
        Careless,
        {
            "extensions": {UsesToken: 1},
            "tokens.api": "sk-live-0123456789",
            "log.output": "stderr",
            "log.format": "json",
        },
    )

    assert "sk-live-0123456789" not in stream.getvalue()
    [warning] = [line for line in _json_lines(stream) if line["level"] == "WARNING"]
    assert warning["message"] == "calling the API with ***"


async def test_the_pernix_logger_is_restored_after_a_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    pernix = logging.getLogger("pernix")
    before = (pernix.propagate, pernix.level, list(pernix.handlers))

    class Idle(Spider):
        pass

    await _crawl(Idle, {"log.output": "stderr", "log.level": "DEBUG"})

    assert (pernix.propagate, pernix.level, list(pernix.handlers)) == before


class StatsUser(Extension):
    def __init__(self, stats: Stats) -> None:
        self.stats = stats

    @events.on(events.SpiderOpened)
    def opened(self, _event: events.SpiderOpened) -> None:
        self.stats.set("ext.opened", "yes")


async def test_stats_are_injected_into_components() -> None:
    class Idle(Spider):
        pass

    crawler = await _crawl(Idle, {"extensions": {StatsUser: 1}})

    assert crawler.stats.get("ext.opened") == "yes"
