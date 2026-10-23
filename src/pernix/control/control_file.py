"""The built-in control source: the state a Platform writes in a file."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.control.control_source import CONTROL_STATES

if TYPE_CHECKING:
    from pernix.control.control_source import ControlState

__all__ = ["ControlFile", "read_control_file"]

logger = logging.getLogger(__name__)

_MAX_BYTES = 1024
"""Bytes read from a control file at most; a state is a few."""

_STOPS: frozenset[ControlState] = frozenset({"drain", "force"})

type _Version = tuple[int, int, int, int]
"""A version of a file: its device, inode, modification time in nanoseconds, and size, each new write changing it."""


class ControlFile:
    """The control state written in a file, read anew each time; see `read_control_file()`.

    `control.file` sets one, read before the sources of `control.sources`; with
    `job_dir`, it is `control` in the job directory by default. Write the file
    atomically, to a temporary file in the same directory renamed over it, such as
    with `printf paused > state.tmp && mv state.tmp state`; a file written in place
    may be read empty or cut, which is ignored.
    """

    def __init__(self, path: str | os.PathLike[str], *, left: os.stat_result | None = None) -> None:
        """Name the file.

        Args:
            path: The file.
            left: The file as an earlier process left it, as `os.stat()` tells, such
                as the control file of a job directory when the Run took it. A
                `drain` or `force` read from that version of the file was written for
                that process, so it is logged once and told as `running`, until the
                file is written again, even with the same state; a `paused` is told.
        """
        self._path = Path(path)
        self._left = None if left is None else _version(left)
        """The version of the file whose stops are not for this process, until another version is read."""
        self._warned = False

    async def state(self) -> ControlState:
        """Read the state of the file, off the event loop.

        Returns:
            The state.

        Raises:
            ValueError: The file is empty, or holds something else.
            OSError: The file cannot be read.
        """
        state, version = await asyncio.to_thread(_read, self._path)
        if version != self._left:
            self._left = None  # written again since
        if self._left is None or state not in _STOPS:
            return state
        if not self._warned:
            self._warned = True
            logger.warning(
                "The control file %s holds %s, written before this process took the job directory; it is ignored "
                "until the file is written again",
                self._path,
                state,
            )
        return "running"

    def __repr__(self) -> str:
        return f"ControlFile({str(self._path)!r})"


def read_control_file(path: str | os.PathLike[str]) -> ControlState:
    """Read the control state a file holds: one of `running`, `paused`, `drain`, or `force`.

    Whitespace around the state and its case are ignored. A file that does not exist
    holds `running`, so a Platform that never wrote one leaves the Run running.

    Args:
        path: The file.

    Returns:
        The state.

    Raises:
        ValueError: The file is empty, as while it is written in place, holds
            something other than a state, or is longer than 1 KiB.
        OSError: The file exists but cannot be read.
    """
    return _read(path)[0]


def _read(path: str | os.PathLike[str]) -> tuple[ControlState, _Version | None]:
    """Read the state a file holds, with the version of the file read; `None` for a file that does not exist."""
    try:
        with Path(path).open("rb") as file:
            # First, so that a write in between delays a stop by a read, rather than making an old one new.
            version = _version(os.fstat(file.fileno()))
            data = file.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return "running", None
    expected = ", ".join(CONTROL_STATES[:-1]) + f", or {CONTROL_STATES[-1]}"
    if len(data) > _MAX_BYTES:  # its start alone may look like a state
        raise ValueError(f"{path} holds more than {_MAX_BYTES} bytes; it should hold {expected}")
    text = data.decode("utf-8", errors="replace").strip().lower()
    for state in CONTROL_STATES:
        if text == state:
            return state, version
    if not text:
        raise ValueError(f"{path} is empty; it should hold {expected}")
    shown = text if len(text) <= 40 else f"{text[:40]}..."
    raise ValueError(f"{path} holds {shown!r}, not {expected}")


def _version(status: os.stat_result) -> _Version:
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)
