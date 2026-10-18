from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _in_empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("browser_engine")
def test_the_browser_is_launched_and_named(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0

    lines = capsys.readouterr().out.splitlines()
    fetchers = lines.index("Fetchers: http (HTTPFetcher), browser (BrowserFetcher)")
    assert lines[fetchers + 1].startswith("Browser: chromium ")


@pytest.mark.usefixtures("without_browser_engine")
def test_a_missing_browser_engine_is_reported_with_the_command_that_installs_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check"]) == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert err == "Error: the browser engine is not installed; install it with: pernix browser install\n"


def test_a_browser_that_does_not_start_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "-s", 'browser.launch_options={"executable_path": "/usr/bin/false"}']) == 2

    assert capsys.readouterr().err.startswith("Error: the browser does not start: ")


@pytest.mark.usefixtures("without_patchright")
def test_without_the_extra_the_browser_is_noted_but_not_a_problem(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert "Browser: not installed (rendering needs patchright; install it with: pip install 'pernix[browser]')" in out


@pytest.mark.usefixtures("without_browser_engine")
def test_without_the_browser_fetcher_the_browser_is_not_checked(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "-s", 'fetchers={"browser": false}']) == 0

    out = capsys.readouterr().out.splitlines()
    assert "Fetchers: http (HTTPFetcher)" in out
    assert not [line for line in out if line.startswith("Browser:")]
