from __future__ import annotations

import io
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from loopback import LoopbackServer


@pytest.fixture
def site(server: LoopbackServer) -> LoopbackServer:
    server.route("/1", "<h1>Dune</h1>", headers=[("Content-Type", "text/html")])
    server.route("/2", "<h1>Emma</h1>", headers=[("Content-Type", "text/html")])
    return server


def _type(monkeypatch: pytest.MonkeyPatch, lines: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(textwrap.dedent(lines)))


def test_the_shell_fetches_first_and_awaits_at_the_top_level(
    site: LoopbackServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _type(
        monkeypatch,
        f"""\
        print("first", response.status, response.css("h1::text").get())
        later = await fetch("{site.url("/2")}")
        print("second", later is response, request.url, response.css("h1::text").get())
        print("settings", settings["concurrency"])
        """,
    )

    assert main(["shell", site.url("/1"), "-s", "concurrency=3"]) == 0

    out, err = capsys.readouterr()
    assert "first 200 Dune" in out
    assert f"second True {site.url('/2')} Emma" in out
    assert "settings 3" in out
    assert f"response: <Response 200 {site.url('/1')}>" in err  # the banner


def test_errors_are_shown_and_the_shell_goes_on(
    site: LoopbackServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _type(
        monkeypatch,
        """\
        print("before", response)
        await fetch("/relative")
        1 / 0
        print("after")
        exit()
        print("never")
        """,
    )

    assert main(["shell"]) == 0

    out, err = capsys.readouterr()
    assert "before None" in out
    assert "ValueError" in err
    assert "ZeroDivisionError" in err
    assert "after" in out
    assert "never" not in out


def test_a_failed_first_fetch_still_opens_the_shell(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _type(monkeypatch, 'print("open", response)\n')

    assert main(["shell", "http://127.0.0.1:9/"]) == 0

    out, err = capsys.readouterr()
    assert "open None" in out
    assert "Error: fetching http://127.0.0.1:9/ failed: ConnectError" in err


def test_exits_and_interrupts_raised_by_awaited_code_reach_the_console() -> None:
    lines = "import asyncio\nawait asyncio.sleep(0); raise KeyboardInterrupt\nprint('alive')\nawait asyncio.sleep(0); exit()\nprint('never')\n"
    process = subprocess.run(  # this interpreter, running the command under test
        [sys.executable, "-m", "pernix", "shell"], input=lines, capture_output=True, text=True, timeout=30, check=False
    )

    assert process.returncode == 0, process.stderr
    assert "KeyboardInterrupt" in process.stderr
    assert "alive" in process.stdout
    assert "never" not in process.stdout
