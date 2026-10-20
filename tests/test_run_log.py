from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import sys
import textwrap
import threading
from typing import Literal

import orjson
import pytest

from pernix.errors import SettingsError
from pernix.logs import RunContext, RunLog, request_context, run_context
from pernix.settings import LogSettings, load_settings
from pernix.stats import LoopLag

RUN = RunContext("run-1", "books")
log = logging.getLogger("pernix.tests")


class BlockedStream(io.StringIO):
    """An output whose writes wait until it is unblocked, like a stalled pipe."""

    def __init__(self) -> None:
        super().__init__()
        self.unblock = threading.Event()

    def write(self, s: str) -> int:
        self.unblock.wait()
        return super().write(s)


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def pernix_logger() -> logging.Logger:
    return logging.getLogger("pernix")


async def test_a_blocked_output_never_stalls_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = BlockedStream()
    monkeypatch.setattr(sys, "stderr", stream)
    run_log = RunLog(LogSettings(output="stderr", queue_size=10), RUN, drain_timeout=0.05)
    lag = LoopLag(interval=0.005)
    try:
        async with asyncio.timeout(5), run_log:
            lag.start()
            with run_context(RUN):
                for n in range(2000):
                    log.warning("record %d", n)
                    if n % 100 == 0:
                        await asyncio.sleep(0)
            await asyncio.sleep(0.02)
            await lag.stop()
        # closing returned after its drain timeout, although the output still blocks
    finally:
        stream.unblock.set()

    assert lag.stats().lag_max < 0.2
    assert run_log.counts.warning == 2000
    assert run_log.counts.dropped >= 2000 - 10 - 2  # the queue holds 10; the writer took one or two


async def test_records_become_redacted_json_lines_with_their_run_and_request(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    run_log = RunLog(LogSettings(output="stderr", format="json"), RUN, secrets=["sk-live-123"])

    async with run_log:
        with run_context(RUN):
            log.info("outside a Request")
            with request_context("17"):
                log.error("fetching via %s with %s failed", "http://alice:s3cret@proxy:8080", "sk-live-123")

    lines = [orjson.loads(line) for line in stream.getvalue().splitlines()]
    assert [(line["run_id"], line["spider"], line["request_id"]) for line in lines] == [
        ("run-1", "books", None),
        ("run-1", "books", "17"),
    ]
    assert lines[1]["message"] == "fetching via http://***@proxy:8080 with *** failed"
    assert lines[1]["logger"] == "pernix.tests"
    assert (run_log.counts.error, run_log.counts.warning, run_log.counts.dropped) == (1, 0, 0)


async def test_values_to_mask_can_be_added_once_the_run_started(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    async with RunLog(LogSettings(output="stderr", format="json"), RUN, secrets=["first"]) as run_log:
        with run_context(RUN):
            run_log.mask(["second", "first"])
            log.info("logging in with first and second")

    [line] = [orjson.loads(line) for line in stream.getvalue().splitlines()]
    assert line["message"] == "logging in with *** and ***"


async def test_exceptions_are_rendered_before_queueing_and_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    async with RunLog(LogSettings(output="stderr", format="json"), RUN):
        with run_context(RUN):
            try:
                raise ConnectionError("proxy http://alice:s3cret@proxy:8080 refused")
            except ConnectionError:
                log.exception("fetch failed")

    [line] = [orjson.loads(line) for line in stream.getvalue().splitlines()]
    assert line["exception"].endswith("ConnectionError: proxy http://***@proxy:8080 refused")


async def test_strings_inside_extra_fields_are_redacted_without_changing_them(monkeypatch: pytest.MonkeyPatch) -> None:
    json_stream, text_stream = io.StringIO(), io.StringIO()
    stats = {"api.proxy": "http://alice:s3cret@proxy:8080", "api.keys": ["sk-live-123"], "items.scraped": 2}

    outputs: list[tuple[io.StringIO, Literal["json", "text"]]] = [(json_stream, "json"), (text_stream, "text")]
    for stream, log_format in outputs:
        monkeypatch.setattr(sys, "stderr", stream)
        async with RunLog(LogSettings(output="stderr", format=log_format), RUN, secrets=["sk-live-123"]):
            with run_context(RUN):
                log.info("Run finished", extra={"stats": stats, "final": True})

    [line] = [orjson.loads(line) for line in json_stream.getvalue().splitlines()]
    assert line["stats"] == {"api.proxy": "http://***@proxy:8080", "api.keys": ["***"], "items.scraped": 2}
    assert text_stream.getvalue().endswith(
        "Run finished\n  api.proxy: http://***@proxy:8080\n  api.keys: ['***']\n  items.scraped: 2\n"
    )
    assert stats["api.proxy"] == "http://alice:s3cret@proxy:8080"  # e.g. a snapshot Stats Sinks also receive


async def test_records_with_cyclic_extra_fields_are_still_written(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    cyclic: dict[str, object] = {"token": "abc"}
    cyclic["self"] = cyclic

    async with RunLog(LogSettings(output="stderr", format="json"), RUN):
        with run_context(RUN):
            log.warning("cyclic", extra={"state": cyclic})

    [line] = [orjson.loads(line) for line in stream.getvalue().splitlines()]
    assert (line["message"], line["state"]) == ("cyclic", {"token": "abc", "self": "..."})


def test_a_blocked_output_does_not_keep_the_process_from_exiting() -> None:
    script = textwrap.dedent("""
        import asyncio, io, logging, sys, threading
        from pernix.logs import RunContext, RunLog, run_context
        from pernix.settings import LogSettings

        class Blocked(io.StringIO):
            def write(self, s):
                threading.Event().wait()
                return 0

        async def main():
            sys.stderr = Blocked()
            run = RunContext("r", "s")
            async with RunLog(LogSettings(output="stderr"), run, drain_timeout=0.1):
                with run_context(run):
                    logging.getLogger("pernix.x").warning("never written")
            print("closed")

        asyncio.run(main())
    """)

    finished = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False)  # noqa: S603

    assert (finished.returncode, finished.stdout) == (0, "closed\n")


async def test_each_run_writes_only_records_at_its_own_level(monkeypatch: pytest.MonkeyPatch) -> None:
    verbose_run, quiet_run = RunContext("a", "verbose"), RunContext("b", "quiet")
    verbose_stream, quiet_stream = io.StringIO(), io.StringIO()
    verbose = RunLog(LogSettings(output="stderr", format="text", level="DEBUG"), verbose_run)
    quiet = RunLog(LogSettings(output="stderr", format="text", level="ERROR"), quiet_run)
    monkeypatch.setattr(sys, "stderr", verbose_stream)
    verbose.open()
    monkeypatch.setattr(sys, "stderr", quiet_stream)
    quiet.open()
    try:
        for run in (verbose_run, quiet_run):
            with run_context(run):
                log.debug("debug")
                log.error("error")
    finally:
        await verbose.close()
        await quiet.close()

    assert _messages(verbose_stream) == ["debug", "error"]
    assert _messages(quiet_stream) == ["error"]
    assert (quiet.counts.error, quiet.counts.warning) == (1, 0)


async def test_child_logger_levels_do_not_lower_the_run_level(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    chatty = logging.getLogger("pernix.tests.chatty")
    monkeypatch.setattr(chatty, "level", logging.DEBUG)

    async with RunLog(LogSettings(output="stderr", format="text", level="INFO"), RUN):
        with run_context(RUN):
            chatty.debug("debug")
            chatty.info("info")

    assert _messages(stream) == ["info"]


async def test_records_go_to_the_root_handlers_when_the_application_configured_logging(
    caplog: pytest.LogCaptureFixture, pernix_logger: logging.Logger
) -> None:
    caplog.set_level(logging.INFO, logger="pernix")
    before = (pernix_logger.propagate, pernix_logger.level)

    async with RunLog(LogSettings(), RUN):
        assert pernix_logger.propagate is False
        assert pernix_logger.level == before[1]  # the application's levels apply
        with run_context(RUN), request_context("3"):
            log.info("token=abc")
            log.debug("below the level")

    assert (pernix_logger.propagate, pernix_logger.level) == before
    [record] = [r for r in caplog.records if r.name == "pernix.tests"]
    assert record.getMessage() == "token=***"
    assert (record.__dict__["run_id"], record.__dict__["request_id"]) == ("run-1", "3")


async def test_pernix_writes_its_own_output_when_logging_is_not_configured(
    monkeypatch: pytest.MonkeyPatch, pernix_logger: logging.Logger
) -> None:
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    before = (pernix_logger.propagate, pernix_logger.level)

    async with RunLog(LogSettings(level="debug"), RUN):
        assert (pernix_logger.propagate, pernix_logger.level) == (False, logging.DEBUG)
        with run_context(RUN):
            log.debug("hello")

    assert (pernix_logger.propagate, pernix_logger.level) == before
    assert terminal.getvalue().endswith(" DEBUG    [pernix.tests] hello\n")  # text on a terminal


async def test_json_lines_go_to_stdout_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    async with RunLog(LogSettings(output="stdout"), RUN):
        with run_context(RUN):
            log.warning("hello")

    assert orjson.loads(stdout.getvalue())["message"] == "hello"  # JSON lines off a terminal
    assert stderr.getvalue() == ""


async def test_each_run_writes_only_its_own_records(monkeypatch: pytest.MonkeyPatch) -> None:
    first_run, second_run = RunContext("a", "one"), RunContext("b", "two")
    first_stream, second_stream = io.StringIO(), io.StringIO()
    first = RunLog(LogSettings(output="stderr", format="text"), first_run)
    second = RunLog(LogSettings(output="stderr", format="text"), second_run)
    monkeypatch.setattr(sys, "stderr", first_stream)
    first.open()
    monkeypatch.setattr(sys, "stderr", second_stream)
    second.open()
    try:
        with run_context(first_run):
            log.warning("for one")
        with run_context(second_run):
            log.warning("for two")
        log.warning("outside a Run, while one is open first")
        await first.close()
        log.warning("outside a Run, after one closed")
    finally:
        await first.close()
        await second.close()

    assert _messages(first_stream) == ["for one", "outside a Run, while one is open first"]
    assert _messages(second_stream) == ["for two", "outside a Run, after one closed"]


async def test_a_run_log_opens_once() -> None:
    run_log = RunLog(LogSettings(output="stderr"), RUN)
    async with run_log:
        with pytest.raises(RuntimeError, match="opens only once"):
            run_log.open()
    await run_log.close()  # closing again does nothing


def test_log_levels_must_be_known() -> None:
    assert LogSettings(level="debug").level == "debug"
    with pytest.raises(SettingsError, match=r"log\.level: unknown level 'loud'"):
        load_settings(overrides={"log.level": "loud"})


def _messages(stream: io.StringIO) -> list[str]:
    return [line.split("] ", 1)[1] for line in stream.getvalue().splitlines()]
