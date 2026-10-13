"""`Spider.run()` as the main program: its result, its exit codes, and the stop signals."""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Response, Spider
from pernix.runner import ExitCode

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from loopback import LoopbackServer

SETTINGS = {"concurrency": 1, "stats.interval": 0}


@pytest.fixture
def pages(server: LoopbackServer) -> LoopbackServer:
    for n in range(1, 6):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    return server


def _pages(server: LoopbackServer) -> type[Spider]:
    class Pages(Spider):
        start_urls = tuple(server.url(f"/{n}") for n in range(1, 6))

        def __init__(self, limit: int = 5) -> None:
            self.limit = limit

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page = int(response.css("h1::text").get() or 0)
            if page <= self.limit:
                yield {"page": page}

    return Pages


def test_run_returns_the_result_of_a_finished_run(pages: LoopbackServer, tmp_path: Path) -> None:
    original = signal.getsignal(signal.SIGINT)

    result = _pages(pages).run(settings=SETTINGS, output=tmp_path / "items.jsonl", limit="2")

    assert result.reason == "finished"
    assert result.stats["items.scraped"] == 2
    assert signal.getsignal(signal.SIGINT) is original


def test_invalid_arguments_exit_with_2_without_a_traceback(
    pages: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        _pages(pages).run(settings=SETTINGS, limit="all")

    assert raised.value.code == ExitCode.CONFIGURATION_ERROR == 2
    assert capsys.readouterr().err == "Error: Invalid Spider arguments:\n  - limit: Expected `int`, got `str`\n"
    assert pages.received == []


class NotAPipeline:
    pass


def test_invalid_components_exit_with_2_and_are_logged_without_a_traceback(
    pages: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(SystemExit) as raised:
        _pages(pages).run(settings={**SETTINGS, "pipelines": {NotAPipeline: 100}})

    assert raised.value.code == 2
    [record] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "NotAPipeline" in record.getMessage()
    assert (record.exc_info, record.exc_text) == (None, None)
    assert pages.received == []


def test_a_failed_run_exits_with_1_after_logging_the_error(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Raw(Spider):
        start_urls = (pages.url("/1"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"body": response.body}  # bytes have no form in JSON

    with pytest.raises(SystemExit) as raised:
        Raw.run(settings=SETTINGS, output=tmp_path / "items.jsonl")

    assert raised.value.code == ExitCode.FAILED == 1
    [record] = [record for record in caplog.records if record.getMessage() == "The Run failed"]
    assert "FeedError: cannot write an Item" in (record.exc_text or "")


SCRIPT = """
import asyncio
import pathlib
import sys

from pernix import Spider

BASE, MARKER, WAIT, OUTPUT, LIMIT = sys.argv[1:]


class Pages(Spider):
    start_urls = [f"{BASE}/{n}" for n in range(1, 6)]
    settings = {"concurrency": 1, "stats.interval": 0}

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit

    async def parse(self, response):
        pathlib.Path(MARKER).touch()
        await asyncio.sleep(float(WAIT))
        yield {"page": response.css("h1::text").get()}


if __name__ == "__main__":
    result = Pages.run(output=OUTPUT, limit=LIMIT)
    print("finished as", result.reason)
"""


class Program:
    """A Spider script running in a subprocess, with its stderr collected as it comes."""

    def __init__(self, server: LoopbackServer, directory: Path, *, wait: float, limit: str = "5") -> None:
        script = directory / "pages.py"
        script.write_text(SCRIPT)
        self.marker = directory / "fetched"
        self.output = directory / "items.jsonl"
        self.process = subprocess.Popen(  # noqa: S603  # our own script, run by this interpreter
            [sys.executable, str(script), server.base_url, str(self.marker), str(wait), str(self.output), limit],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.stderr: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stderr is not None
        with self.process.stderr:
            self.stderr.extend(iter(self.process.stderr.readline, ""))

    def wait_until_fetching(self) -> None:
        deadline = time.monotonic() + 20
        while not self.marker.exists():
            assert self.process.poll() is None, "".join(self.stderr)
            assert time.monotonic() < deadline, "the Spider never fetched"
            time.sleep(0.02)

    def wait_for_log(self, text: str) -> None:
        deadline = time.monotonic() + 20
        while not any(text in line for line in self.stderr):
            assert time.monotonic() < deadline, "".join(self.stderr)
            time.sleep(0.02)

    def finish(self) -> tuple[int, str, str]:
        code = self.process.wait(timeout=20)
        self._reader.join(timeout=5)
        assert self.process.stdout is not None
        with self.process.stdout:
            stdout = self.process.stdout.read()
        return code, stdout, "".join(self.stderr)

    def items(self) -> list[Any]:
        return [orjson.loads(line) for line in self.output.read_text().splitlines()]


posix_signals = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def test_a_completed_program_exits_with_0(pages: LoopbackServer, tmp_path: Path) -> None:
    program = Program(pages, tmp_path, wait=0)

    code, stdout, _ = program.finish()

    assert (code, stdout) == (ExitCode.COMPLETED, "finished as finished\n")
    assert len(program.items()) == 5


def test_invalid_arguments_end_the_program_with_2(pages: LoopbackServer, tmp_path: Path) -> None:
    program = Program(pages, tmp_path, wait=0, limit="all")

    code, stdout, stderr = program.finish()

    assert (code, stdout) == (ExitCode.CONFIGURATION_ERROR, "")
    assert "Error: Invalid Spider arguments:\n  - limit: Expected `int`, got `str`\n" in stderr
    assert "Traceback" not in stderr
    assert pages.received == []


@posix_signals
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_one_signal_drains_the_run_and_exits_with_130(
    pages: LoopbackServer, tmp_path: Path, signum: signal.Signals
) -> None:
    program = Program(pages, tmp_path, wait=0.5)
    program.wait_until_fetching()

    program.process.send_signal(signum)
    code, stdout, stderr = program.finish()

    assert (code, stdout) == (ExitCode.INTERRUPTED, "")
    assert f"Received {signum.name}: draining the Run; send it again to stop now" in stderr
    assert program.items() == [{"page": "1"}]  # the Request in flight was finished
    assert [request.path for request in pages.received] == ["/1"]
    assert "Run drained after" in stderr


@posix_signals
def test_a_second_signal_force_stops_the_run_and_exits_with_130(pages: LoopbackServer, tmp_path: Path) -> None:
    program = Program(pages, tmp_path, wait=60)
    program.wait_until_fetching()

    program.process.send_signal(signal.SIGINT)
    program.wait_for_log("draining the Run")
    started = time.monotonic()
    program.process.send_signal(signal.SIGINT)
    code, stdout, stderr = program.finish()

    assert (code, stdout) == (ExitCode.INTERRUPTED, "")
    assert time.monotonic() - started < 10
    assert "Received SIGINT while draining: stopping the Run now" in stderr
    assert "Run forced after" in stderr
    assert program.items() == []  # the Feed is complete, if empty
    assert sorted(path.name for path in tmp_path.iterdir()) == ["fetched", "items.jsonl", "pages.py"]
