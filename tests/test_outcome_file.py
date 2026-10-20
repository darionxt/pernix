"""The outcome file: how each way a Run ends is written, masked, and moved into place."""

from __future__ import annotations

import asyncio
import datetime as dt
import signal
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Extension, Response, Spider
from pernix.crawler import Crawler
from pernix.errors import ComponentError, FeedError, SettingsError, SpiderArgumentError
from pernix.logs import Redactor
from pernix.outcome_file import OutcomeFile
from pernix.runner import ExitCode
from pernix.settings import Secret, Section, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from pernix.shutdown import StopMode
    from pernix.stats import Stats
    from pernix.testing import LoopbackServer

SETTINGS = {"concurrency": 1, "stats.interval": 0}


@pytest.fixture
def pages(server: LoopbackServer) -> LoopbackServer:
    for n in range(1, 4):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    return server


def _pages(
    server: LoopbackServer, *, stop: StopMode | None = None, entered: asyncio.Event | None = None
) -> type[Spider]:
    """A Spider over three pages that stops its Run in its first Callback, or waits there once it has `entered`."""

    class Pages(Spider):
        name = "pages"
        start_urls = tuple(server.url(f"/{n}") for n in range(1, 4))

        def __init__(self, limit: int = 3) -> None:
            self.limit = limit

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if stop is not None:
                self.crawler.stop(stop)
            if stop == "force":
                await asyncio.Event().wait()  # cancelled
            if entered is not None:
                entered.set()
                await asyncio.Event().wait()
            page = int(response.css("h1::text").get() or 0)
            if page <= self.limit:
                yield {"page": page}

    return Pages


def _crawler(spider: type[Spider], settings: Mapping[str, Any], **options: Any) -> Crawler:
    return Crawler.from_spider(spider, settings={**SETTINGS, **settings}, **options)


def _outcome(path: Path) -> dict[str, Any]:
    return orjson.loads(path.read_bytes())


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


async def test_a_finished_run_writes_how_it_ended(pages: LoopbackServer, tmp_path: Path) -> None:
    template = str(tmp_path / "runs" / "{name}" / "{run_id}.json")
    crawler = _crawler(_pages(pages), {"outcome_file": template, "run_id": "r1"})

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    outcome = _outcome(tmp_path / "runs" / "pages" / "r1.json")
    assert list(outcome) == [
        "schema",
        "run_id",
        "spider",
        "reason",
        "exit_code",
        "started_at",
        "finished_at",
        "error",
        "stats",
    ]
    assert {key: outcome[key] for key in ("schema", "run_id", "spider", "reason", "exit_code", "error")} == {
        "schema": 1,
        "run_id": "r1",
        "spider": "pages",
        "reason": "finished",
        "exit_code": 0,
        "error": None,
    }
    started, finished = (dt.datetime.fromisoformat(outcome[key]) for key in ("started_at", "finished_at"))
    assert started == crawler.started_at.replace(microsecond=crawler.started_at.microsecond // 1000 * 1000)
    assert started <= finished <= dt.datetime.now(dt.UTC)
    assert outcome["started_at"].endswith("+00:00")
    assert outcome["stats"] == result.stats
    assert (outcome["stats"]["items.scraped"], outcome["stats"]["run.reason"]) == (3, "finished")
    assert _names(tmp_path / "runs" / "pages") == ["r1.json"]  # no temporary file is left


@pytest.mark.parametrize(("mode", "reason"), [("drain", "drained"), ("force", "forced")])
async def test_a_run_stopped_by_code_completes_with_0(
    pages: LoopbackServer, tmp_path: Path, mode: StopMode, reason: str
) -> None:
    crawler = _crawler(_pages(pages, stop=mode), {"outcome_file": str(tmp_path / "outcome.json")})

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    outcome = _outcome(tmp_path / "outcome.json")
    assert result.reason == reason
    assert (outcome["reason"], outcome["exit_code"], outcome["error"]) == (reason, 0, None)


async def test_a_force_stop_before_the_run_starts_is_written_too(pages: LoopbackServer, tmp_path: Path) -> None:
    crawler = _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json")})

    crawler.stop("force")
    async with asyncio.timeout(10):
        await crawler.crawl()

    outcome = _outcome(tmp_path / "outcome.json")
    assert (outcome["reason"], outcome["exit_code"], outcome["stats"]["run.reason"]) == ("forced", 0, "forced")
    assert pages.received == []


async def test_a_stop_a_signal_asked_for_records_130(pages: LoopbackServer, tmp_path: Path) -> None:
    crawler = _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json")})

    crawler.stop("drain", signal=signal.SIGTERM)
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    outcome = _outcome(tmp_path / "outcome.json")
    assert result.reason == "drained"
    assert (outcome["reason"], outcome["exit_code"]) == ("drained", 130)


async def test_a_failed_run_records_its_error(pages: LoopbackServer, tmp_path: Path) -> None:
    class Raw(Spider):
        name = "raw"
        start_urls = (pages.url("/1"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"body": response.body}  # bytes have no form in JSON

    crawler = _crawler(Raw, {"outcome_file": str(tmp_path / "outcome.json")}, output=tmp_path / "items.jsonl")

    with pytest.RaisesGroup(FeedError):
        await crawler.crawl()

    outcome = _outcome(tmp_path / "outcome.json")
    assert (outcome["reason"], outcome["exit_code"]) == ("failed", 1)
    assert outcome["error"]["type"] == "FeedError"  # the only error of the group
    assert outcome["error"]["message"].startswith(f"cannot write an Item to {tmp_path / 'items.jsonl'}: ")
    assert outcome["stats"]["run.reason"] == "failed"


async def test_a_cancelled_run_records_130(pages: LoopbackServer, tmp_path: Path) -> None:
    entered = asyncio.Event()
    crawler = _crawler(_pages(pages, entered=entered), {"outcome_file": str(tmp_path / "outcome.json")})
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    outcome = _outcome(tmp_path / "outcome.json")
    assert (outcome["reason"], outcome["exit_code"], outcome["error"]) == ("cancelled", 130, None)


async def test_a_stream_left_early_records_a_force_stop(pages: LoopbackServer, tmp_path: Path) -> None:
    crawler = _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json")})

    async with asyncio.timeout(10), crawler.stream_items() as items:
        async for _ in items:
            break

    outcome = _outcome(tmp_path / "outcome.json")
    assert (outcome["reason"], outcome["exit_code"]) == ("forced", 0)


class NotAPipeline:
    pass


async def test_a_configuration_error_found_by_the_run_records_2(pages: LoopbackServer, tmp_path: Path) -> None:
    crawler = _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json"), "pipelines": {NotAPipeline: 1}})

    with pytest.raises(ComponentError, match="NotAPipeline"):
        await crawler.crawl()

    outcome = _outcome(tmp_path / "outcome.json")
    assert (outcome["reason"], outcome["exit_code"], outcome["error"]["type"]) == ("failed", 2, "ComponentError")
    assert "NotAPipeline" in outcome["error"]["message"]
    assert pages.received == []


def test_an_error_found_before_the_run_writes_none(pages: LoopbackServer, tmp_path: Path) -> None:
    with pytest.raises(SpiderArgumentError):
        _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json")}, args={"limit": "all"})

    assert _names(tmp_path) == []


def test_run_exits_with_the_code_the_file_records(pages: LoopbackServer, tmp_path: Path) -> None:
    settings = {**SETTINGS, "outcome_file": str(tmp_path / "outcome.json"), "pipelines": {NotAPipeline: 1}}

    with pytest.raises(SystemExit) as raised:
        _pages(pages).run(settings=settings)

    assert raised.value.code == _outcome(tmp_path / "outcome.json")["exit_code"] == ExitCode.CONFIGURATION_ERROR


async def test_without_the_setting_nothing_is_written(
    pages: LoopbackServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    async with asyncio.timeout(10):
        await _crawler(_pages(pages), {}, output="items.jsonl").crawl()

    assert _names(tmp_path) == ["items.jsonl"]


class TokenSettings(Section):
    __prefix__ = "outcome_tokens"
    api: Secret = Secret("")


class RefusedLogin(Extension):
    """Fails while opening, leaving the API token in its error and a proxy's credentials in the stats."""

    def __init__(self, tokens: TokenSettings, stats: Stats) -> None:
        self.tokens = tokens
        self.stats = stats

    async def open(self) -> None:
        self.stats.set("login.proxy", "http://alice:pa55@proxy.test:8080")
        raise RuntimeError(f"the API refused {self.tokens.api.reveal()}")


async def test_secrets_in_the_error_and_the_stats_are_masked(pages: LoopbackServer, tmp_path: Path) -> None:
    settings = {
        "outcome_file": str(tmp_path / "outcome.json"),
        "extensions": {RefusedLogin: 1},
        "outcome_tokens.api": "sk-live-0123456789",
    }

    with pytest.raises(RuntimeError, match="sk-live"):
        await _crawler(_pages(pages), settings).crawl()

    text = (tmp_path / "outcome.json").read_text()
    outcome = orjson.loads(text)
    assert "sk-live" not in text
    assert "pa55" not in text
    assert outcome["error"] == {"type": "RuntimeError", "message": "the API refused ***"}
    assert outcome["stats"]["login.proxy"] == "http://***@proxy.test:8080"
    assert (outcome["reason"], outcome["exit_code"]) == ("failed", 1)


async def test_an_existing_file_is_replaced(pages: LoopbackServer, tmp_path: Path) -> None:
    (tmp_path / "outcome.json").write_text("from an earlier Run")

    async with asyncio.timeout(10):
        await _crawler(_pages(pages), {"outcome_file": str(tmp_path / "outcome.json")}).crawl()

    assert _outcome(tmp_path / "outcome.json")["reason"] == "finished"
    assert _names(tmp_path) == ["outcome.json"]


async def test_a_file_that_cannot_be_written_is_logged_and_changes_nothing_else(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "taken").write_text("a file, not a directory")

    async with asyncio.timeout(10):
        result = await _crawler(_pages(pages), {"outcome_file": str(tmp_path / "taken" / "outcome.json")}).crawl()

    assert result.reason == "finished"
    [record] = [record for record in caplog.records if record.name == "pernix.outcome_file"]
    assert record.getMessage().startswith(f"Cannot write the outcome file {tmp_path / 'taken' / 'outcome.json'}: ")
    assert _names(tmp_path) == ["taken"]


async def test_a_path_that_names_no_file_is_logged_and_changes_nothing_else(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async with asyncio.timeout(10):
        result = await _crawler(_pages(pages), {"outcome_file": tmp_path.anchor}).crawl()

    assert result.reason == "finished"
    [record] = [record for record in caplog.records if record.name == "pernix.outcome_file"]
    assert record.getMessage() == f"Writing the outcome file {tmp_path.anchor} failed"


def test_the_path_is_checked_at_startup() -> None:
    with pytest.raises(SettingsError, match=r"outcome_file: unknown placeholders \['spider'\]"):
        load_settings(overrides={"outcome_file": "{spider}.json"})


@pytest.mark.parametrize(
    ("other", "problem"),
    [
        ("output", "output and outcome_file both write"),
        ("feeds", r"feeds\[.*\] and outcome_file both write"),
        ("items.failed_output", "items.failed_output and outcome_file both write"),
    ],
)
async def test_the_file_never_replaces_another_output(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture, other: str, problem: str
) -> None:
    feed = tmp_path / "pages.jsonl"
    feed.write_text("written by an earlier Run")
    settings: dict[str, Any] = {"outcome_file": str(tmp_path / "{name}.jsonl")}
    if other == "feeds":
        settings["feeds"] = {str(feed): {}}
    elif other == "items.failed_output":
        settings["items.failed_output"] = f"{tmp_path}/./pages.jsonl"

    with pytest.raises(SettingsError, match=problem):
        await _crawler(_pages(pages), settings, output=feed if other == "output" else None).crawl()

    assert feed.read_text() == "written by an earlier Run"
    assert "The outcome file is not written: " in caplog.text


def _write(path: Path, error: BaseException, *, stats: Mapping[str, Any] | None = None) -> None:
    """Write the outcome file of a failed Run of an idle Spider, with these stats."""

    class Idle(Spider):
        name = "idle"

    crawler = Crawler.from_spider(Idle)
    for key, value in (stats or {}).items():
        crawler.stats.set(key, value)
    OutcomeFile(crawler, path).write("failed", error=error, redactor=Redactor())


def _written(path: Path, error: BaseException, *, stats: Mapping[str, Any] | None = None) -> dict[str, Any]:
    _write(path, error, stats=stats)
    return _outcome(path)


def test_a_stat_json_cannot_hold_is_logged_and_changes_nothing_else(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write(tmp_path / "outcome.json", RuntimeError("x"), stats={"text.broken": "\ud800"})

    [record] = [record for record in caplog.records if record.name == "pernix.outcome_file"]
    assert record.getMessage() == f"Writing the outcome file {tmp_path / 'outcome.json'} failed"
    assert "surrogates not allowed" in (record.exc_text or "")
    assert _names(tmp_path) == []


def test_a_group_of_several_errors_lists_them(tmp_path: Path) -> None:
    group = ExceptionGroup("closing failed", [ValueError("bad value"), ExceptionGroup("inner", [OSError("disk full")])])

    outcome = _written(tmp_path / "outcome.json", group)

    assert outcome["error"] == {"type": "ExceptionGroup", "message": "ValueError: bad value; OSError: disk full"}


def test_a_group_of_one_error_is_that_error(tmp_path: Path) -> None:
    group = ExceptionGroup("outer", [ExceptionGroup("inner", [KeyError("k")])])

    assert _written(tmp_path / "outcome.json", group)["error"] == {"type": "KeyError", "message": "'k'"}


def test_stats_beyond_json_numbers_are_written_as_strings(tmp_path: Path) -> None:
    stats = {"big.count": 2**70, "big.max": 2**64 - 1, "big.min": -(2**63), "big.ratio": 0.5}

    outcome = _written(tmp_path / "outcome.json", RuntimeError("x"), stats=stats)

    assert {key: value for key, value in outcome["stats"].items() if key.startswith("big.")} == {
        "big.count": str(2**70),
        "big.max": 2**64 - 1,
        "big.min": -(2**63),
        "big.ratio": 0.5,
    }


@pytest.mark.parametrize(
    ("error", "interrupted", "code"),
    [
        (None, False, ExitCode.COMPLETED),
        (None, True, ExitCode.INTERRUPTED),
        (SettingsError(["bad"]), True, ExitCode.CONFIGURATION_ERROR),
        (RuntimeError("boom"), True, ExitCode.FAILED),
        (asyncio.CancelledError(), False, ExitCode.INTERRUPTED),
        (KeyboardInterrupt(), False, ExitCode.INTERRUPTED),
    ],
)
def test_the_exit_code_follows_the_ending(error: BaseException | None, interrupted: bool, code: ExitCode) -> None:
    assert ExitCode.of(error, interrupted=interrupted) is code
