"""Killing the processes of the browsers a test launched: its renderers, the browser itself, or patchright's driver."""

from __future__ import annotations

import asyncio
import threading

import psutil

from pernix.testing import ReceivedRequest, Reply

HTML = [("Content-Type", "text/html")]

HELD_PAGE = "<p>held</p><img src='/held.png'>"
"""A page whose `load` event waits for an image `Held` holds, with its document shown, in its renderer."""


class Held:
    """A handler that holds its first request until released, and answers the others at once."""

    def __init__(self) -> None:
        self.arrived = threading.Event()
        self.release = threading.Event()
        self._first = True
        self._lock = threading.Lock()

    def __call__(self, _request: ReceivedRequest) -> Reply:
        with self._lock:
            first, self._first = self._first, False
        if first:
            self.arrived.set()
            self.release.wait(30)
        return Reply(b"", headers=[("Content-Type", "image/png")])

    async def wait(self) -> None:
        """Wait until the first request arrived."""
        assert await asyncio.to_thread(self.arrived.wait, 30), "the held request never arrived"


def descendants() -> list[psutil.Process]:
    """The processes this one started, and those they started: drivers, browsers, and their helpers."""
    return psutil.Process().children(recursive=True)


def drivers() -> list[psutil.Process]:
    """The processes of patchright's drivers."""
    return [process for process in descendants() if "run-driver" in _command(process)]


def browsers() -> list[psutil.Process]:
    """The browsers' own processes, which the drivers started."""
    return [child for driver in drivers() for child in _children(driver)]


def renderers() -> list[psutil.Process]:
    """The processes of the browsers' renderers, which run their pages."""
    return [process for process in descendants() if "--type=renderer" in _command(process)]


def kill(processes: list[psutil.Process]) -> None:
    """Kill processes at once, as the system does to a process out of memory."""
    assert processes, "nothing to kill"
    for process in processes:
        process.kill()


async def gone(processes: list[psutil.Process]) -> list[psutil.Process]:
    """Wait up to 10 s for processes to end; return those still running then."""
    _, alive = await asyncio.to_thread(psutil.wait_procs, processes, 10.0)
    return alive


def _command(process: psutil.Process) -> str:
    try:
        return " ".join(process.cmdline())
    except (psutil.Error, SystemError):  # psutil 7.2.2 raises SystemError, on macOS, for a process ending meanwhile
        return ""


def _children(process: psutil.Process) -> list[psutil.Process]:
    try:
        return process.children()
    except psutil.Error:  # it ended meanwhile
        return []
