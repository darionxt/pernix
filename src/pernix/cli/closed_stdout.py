"""What a command does once a reader of its stdout, such as `head`, closed it before the command was done."""

from __future__ import annotations

import contextlib
import os
import sys

__all__ = ["discard_stdout"]


def discard_stdout() -> None:
    """Send what is still written to stdout nowhere, such as what Python flushes at exit, which would fail."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
