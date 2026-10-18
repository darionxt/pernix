"""The processes of a browser, with stand-ins: a process that starts a helper, as a browser starts its renderers."""

from __future__ import annotations

import subprocess
import sys
import time
from typing import TYPE_CHECKING

import psutil
import pytest

from pernix.fetchers.browser_processes import BrowserProcesses

if TYPE_CHECKING:
    from collections.abc import Iterator

STARTS_A_HELPER = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"


@pytest.fixture
def browser() -> Iterator[subprocess.Popen[bytes]]:
    """A process leading a group of its own, as the browser does, with a helper it started."""
    process = subprocess.Popen([sys.executable, "-c", STARTS_A_HELPER], start_new_session=True)  # noqa: S603  # this interpreter
    deadline = time.monotonic() + 10
    while not psutil.Process(process.pid).children() and time.monotonic() < deadline:
        time.sleep(0.05)
    yield process
    for helper in psutil.Process(process.pid).children() if process.poll() is None else []:
        helper.kill()
    process.kill()
    process.wait()


def test_a_browser_and_the_processes_it_started_are_killed(browser: subprocess.Popen[bytes]) -> None:
    processes = BrowserProcesses.of(browser.pid)
    assert processes is not None
    [helper] = psutil.Process(browser.pid).children()

    processes.kill()

    assert browser.wait(10) != 0
    assert psutil.wait_procs([helper], timeout=10)[1] == []


def test_the_helpers_remembered_are_killed_once_the_browser_ended_before_them(browser: subprocess.Popen[bytes]) -> None:
    processes = BrowserProcesses.of(browser.pid)  # remembers its helper
    assert processes is not None
    [helper] = psutil.Process(browser.pid).children()
    browser.kill()
    browser.wait()
    assert helper.is_running()  # left to itself, as a browser's helpers may be

    processes.kill()

    assert psutil.wait_procs([helper], timeout=10)[1] == []


def test_the_memory_of_a_browser_sums_that_of_its_processes(browser: subprocess.Popen[bytes]) -> None:
    processes = BrowserProcesses.of(browser.pid)
    assert processes is not None
    own = psutil.Process(browser.pid).memory_info().rss

    assert processes.memory() > own


def test_a_process_that_took_the_id_of_one_that_ended_is_left_alone(browser: subprocess.Popen[bytes]) -> None:
    processes = BrowserProcesses(browser.pid, psutil.Process(browser.pid).create_time() - 1, None)

    processes.kill()

    assert browser.poll() is None
    assert processes.memory() == 0
