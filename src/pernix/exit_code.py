from __future__ import annotations

import asyncio
import enum

from pernix.errors import CONFIGURATION_ERRORS

__all__ = ["ExitCode"]


class ExitCode(enum.IntEnum):
    """The exit status of a program that runs a Spider."""

    COMPLETED = 0
    """The Run finished, or code stopped it."""
    FAILED = 1
    """An error ended the Run."""
    CONFIGURATION_ERROR = 2
    """The settings, the Spider arguments, or a component are invalid."""
    INTERRUPTED = 130
    """A signal stopped the Run, or its task was cancelled."""

    @classmethod
    def of(cls, error: BaseException | None, *, interrupted: bool = False) -> ExitCode:
        """Tell the exit status of a program whose Run ended so.

        Args:
            error: What ended the Run, if it did not complete: the error it failed
                with, or the cancellation that stopped it.
            interrupted: Whether a stop signal stopped the Run.

        Returns:
            `COMPLETED` for a Run that completed, or `INTERRUPTED` when a signal
            stopped it; `CONFIGURATION_ERROR` for a configuration error;
            `INTERRUPTED` for a cancellation or a `KeyboardInterrupt`, for which
            Python exits with 130 too; and `FAILED` for any other error.
        """
        if error is None:
            return cls.INTERRUPTED if interrupted else cls.COMPLETED
        if isinstance(error, CONFIGURATION_ERRORS):
            return cls.CONFIGURATION_ERROR
        if isinstance(error, asyncio.CancelledError | KeyboardInterrupt):
            return cls.INTERRUPTED
        return cls.FAILED
