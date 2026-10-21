"""The built-in control source: the state a Platform writes in a file."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.control.control_source import CONTROL_STATES

if TYPE_CHECKING:
    import os

    from pernix.control.control_source import ControlState

__all__ = ["ControlFile", "read_control_file"]

_MAX_BYTES = 1024
"""Bytes read from a control file at most; a state is a few."""


class ControlFile:
    """The control state written in a file, read anew each time; see `read_control_file()`.

    `control.file` sets one, read before the sources of `control.sources`. Write the
    file atomically, to a temporary file in the same directory renamed over it, such
    as with `printf paused > state.tmp && mv state.tmp state`; a file written in place
    may be read empty or cut, which is ignored.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Name the file.

        Args:
            path: The file.
        """
        self._path = Path(path)

    async def state(self) -> ControlState:
        """Read the state of the file, off the event loop.

        Returns:
            The state.

        Raises:
            ValueError: The file is empty, or holds something else.
            OSError: The file cannot be read.
        """
        return await asyncio.to_thread(read_control_file, self._path)

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
    try:
        with Path(path).open("rb") as file:
            data = file.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        return "running"
    expected = ", ".join(CONTROL_STATES[:-1]) + f", or {CONTROL_STATES[-1]}"
    if len(data) > _MAX_BYTES:  # its start alone may look like a state
        raise ValueError(f"{path} holds more than {_MAX_BYTES} bytes; it should hold {expected}")
    text = data.decode("utf-8", errors="replace").strip().lower()
    for state in CONTROL_STATES:
        if text == state:
            return state
    if not text:
        raise ValueError(f"{path} is empty; it should hold {expected}")
    shown = text if len(text) <= 40 else f"{text[:40]}..."
    raise ValueError(f"{path} holds {shown!r}, not {expected}")
