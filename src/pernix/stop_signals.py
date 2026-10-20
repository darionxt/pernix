from __future__ import annotations

import asyncio
import logging
import signal
import threading
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType, TracebackType

    from pernix.crawler import Crawler
    from pernix.shutdown import StopMode

    type _Handler = Callable[[int, FrameType | None], Any] | int | signal.Handlers | None

__all__ = ["StopSignals"]

logger = logging.getLogger(__name__)

_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_DEFAULTS: dict[signal.Signals, _Handler] = {
    signal.SIGINT: signal.default_int_handler,
    signal.SIGTERM: signal.SIG_DFL,
}


class StopSignals:
    """Stops a Run on SIGINT and SIGTERM.

    The first signal drains the Run, and the second force-stops it; both name the
    signal to `Crawler.stop()`, so that the Run's outcome file records the exit code
    130 the program exits with. Any further one gets Python's default handling:
    SIGINT raises `KeyboardInterrupt`, and SIGTERM ends the process at once, which
    helps when sync code blocks the event loop.

    Used as a context manager inside the event loop, it installs its handlers on
    entry and restores the previous ones on exit. Signal handlers belong to the main
    thread; elsewhere it does nothing.

    Attributes:
        received: How many of the signals arrived.
    """

    def __init__(self, crawler: Crawler) -> None:
        """Watch for the signals that stop a Run.

        Args:
            crawler: The Crawler of the Run.
        """
        self.received = 0
        self._crawler = crawler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous: dict[signal.Signals, _Handler] = {}

    def __enter__(self) -> Self:
        if threading.current_thread() is not threading.main_thread():
            return self
        self._loop = asyncio.get_running_loop()
        for signum in _SIGNALS:
            self._previous[signum] = signal.signal(signum, self._handle)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        for signum, handler in self._previous.items():
            if handler is not None:  # None: installed outside Python, and cannot be restored
                signal.signal(signum, handler)
        self._previous.clear()

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        # Runs between two bytecodes of the main thread: hand the work to the loop.
        loop = self._loop
        if loop is None:
            return
        self.received += 1
        mode: StopMode = "drain" if self.received == 1 else "force"
        if mode == "force":
            for other, handler in _DEFAULTS.items():
                signal.signal(other, handler)
        loop.call_soon_threadsafe(self._stop, signal.Signals(signum), mode)

    def _stop(self, signum: signal.Signals, mode: StopMode) -> None:
        if mode == "drain":
            logger.warning("Received %s: draining the Run; send it again to stop now", signum.name)
        else:
            logger.warning("Received %s while draining: stopping the Run now", signum.name)
        self._crawler.stop(mode, signal=signum)
