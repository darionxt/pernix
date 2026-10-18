from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable

    Commands = list[list[str]]

_INSTALL = [sys.executable, "-m", "patchright", "install", "--no-shell", "chromium"]


@pytest.fixture
def run_commands(monkeypatch: pytest.MonkeyPatch) -> Callable[[int], Commands]:
    """Record the commands the CLI runs instead of running them, each exiting with the given status."""

    def replace(status: int) -> Commands:
        commands: Commands = []

        def recorded(command: list[str], **_options: Any) -> subprocess.CompletedProcess[bytes]:
            commands.append(command)
            return subprocess.CompletedProcess(command, status)

        monkeypatch.setattr(subprocess, "run", recorded)
        return commands

    return replace


def test_install_installs_the_engine_the_browser_fetcher_launches(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    commands: Commands = []
    run = subprocess.run

    def dry_run(command: list[str], **options: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return run([*command, "--dry-run"], **options)  # patchright tells what it would install, downloading nothing

    monkeypatch.setattr(subprocess, "run", dry_run)

    assert main(["browser", "install"]) == 0

    out, err = capfd.readouterr()
    assert commands == [_INSTALL]
    assert "Chrome for Testing" in out
    assert "headless" not in out.lower()  # the headless shell, which the Fetcher never launches
    assert err.endswith("The browser engine is installed.\n")


def test_install_with_deps_installs_the_system_libraries_too(
    run_commands: Callable[[int], Commands], capsys: pytest.CaptureFixture[str]
) -> None:
    commands = run_commands(0)

    assert main(["browser", "install", "--with-deps"]) == 0

    assert commands == [[*_INSTALL[:4], "--with-deps", *_INSTALL[4:]]]
    assert capsys.readouterr().err == "The browser engine is installed.\n"


def test_a_failed_install_exits_with_1(
    run_commands: Callable[[int], Commands], capsys: pytest.CaptureFixture[str]
) -> None:
    run_commands(3)

    assert main(["browser", "install"]) == 1

    assert capsys.readouterr().err == "Error: installing the browser engine failed: patchright exited with 3\n"


@pytest.mark.usefixtures("without_patchright")
def test_install_without_the_extra_exits_with_2_and_how_to_install_it(
    run_commands: Callable[[int], Commands], capsys: pytest.CaptureFixture[str]
) -> None:
    commands = run_commands(0)

    assert main(["browser", "install"]) == 2

    assert capsys.readouterr().err == (
        "Error: rendering needs patchright; install it with: pip install 'pernix[browser]'\n"
    )
    assert commands == []


def test_browser_needs_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["browser"])

    assert exited.value.code == 2
    assert "the following arguments are required: COMMAND" in capsys.readouterr().err
