from __future__ import annotations

import signal
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.testing import LoopbackServer

    WriteSpider = Callable[[str, str], Path]

PAGES = """
    import asyncio
    import os
    import pathlib

    from pernix import Spider

    BASE = os.environ["CLI_TEST_BASE"]


    class Pages(Spider):
        name = "pages"
        start_urls = [f"{BASE}/{n}" for n in range(1, 4)]
        settings = {"concurrency": 1, "stats.interval": 0}

        def __init__(self, limit: int = 3, wait: float = 0.0) -> None:
            self.limit = limit
            self.wait = wait

        async def parse(self, response):
            pathlib.Path(os.environ["CLI_TEST_MARKER"]).touch()
            await asyncio.sleep(self.wait)
            page = int(response.css("h1::text").get())
            if page <= self.limit:
                yield {"page": page}
"""


@pytest.fixture
def pages(server: LoopbackServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LoopbackServer:
    for n in range(1, 4):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    monkeypatch.setenv("CLI_TEST_BASE", server.base_url)
    monkeypatch.setenv("CLI_TEST_MARKER", str(tmp_path / "fetched"))
    return server


def _items(path: Path) -> list[Any]:
    return [orjson.loads(line) for line in path.read_text().splitlines()]


def test_run_runs_a_spider_with_its_arguments_and_options(
    pages: LoopbackServer, write_spider: WriteSpider, tmp_path: Path
) -> None:
    path = write_spider("cli_pages", PAGES)

    code = main(["run", str(path), "-a", "limit=2", "-o", str(tmp_path / "{run_id}.jsonl"), "--run-id", "r1"])

    assert code == 0
    assert sorted(item["page"] for item in _items(tmp_path / "r1.jsonl")) == [1, 2]


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["-a", "limit=all"], "limit: Expected `int`, got `str`"),
        (["-s", "concurency=2"], "did you mean 'concurrency'?"),
    ],
)
def test_configuration_errors_exit_with_2(
    pages: LoopbackServer,
    write_spider: WriteSpider,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    problem: str,
) -> None:
    path = write_spider("cli_pages_invalid", PAGES)

    assert main(["run", str(path), *argv]) == 2

    assert problem in capsys.readouterr().err
    assert pages.received == []


def test_a_spider_that_cannot_be_found_exits_with_2(
    pages: LoopbackServer, write_spider: WriteSpider, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_spider("cli_pages_missing", PAGES)

    assert main(["run", f"{path}:Books"]) == 2
    assert "defines no Spider named 'Books'; Spiders defined there: Pages (name 'pages')" in capsys.readouterr().err


def test_invalid_options_exit_with_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["run", "spider.py", "-s", "concurrency"])

    assert raised.value.code == 2
    assert "expected KEY=VALUE, got 'concurrency'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2


def _pernix(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603  # this interpreter, running the command under test
        [sys.executable, "-m", "pernix", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def test_the_command_runs_as_a_module_and_as_a_script() -> None:
    module = _pernix("--version")
    out, _ = module.communicate(timeout=20)
    assert (module.returncode, out) == (0, f"pernix {version('pernix')}\n")

    script = Path(sys.executable).with_name("pernix")
    completed = subprocess.run([script, "--version"], capture_output=True, text=True, timeout=20, check=False)  # noqa: S603
    assert (completed.returncode, completed.stdout) == (0, out)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_signal_drains_a_run_started_from_the_command_line(
    pages: LoopbackServer, write_spider: WriteSpider, tmp_path: Path
) -> None:
    path = write_spider("cli_pages_signal", PAGES)
    process = _pernix("run", str(path), "-a", "wait=0.5", "-o", str(tmp_path / "items.jsonl"))
    deadline = time.monotonic() + 20
    while not (tmp_path / "fetched").exists():
        assert process.poll() is None, process.communicate()
        assert time.monotonic() < deadline
        time.sleep(0.02)

    process.send_signal(signal.SIGINT)
    _, err = process.communicate(timeout=20)

    assert process.returncode == 130, err
    assert _items(tmp_path / "items.jsonl") == [{"page": 1}]
    assert "Received SIGINT: draining the Run" in err
