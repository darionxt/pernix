from __future__ import annotations

import contextvars
import logging
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING

from pernix.logs import current_run

if TYPE_CHECKING:
    from types import FrameType

    from pernix.stats.stats import Stats

__all__ = ["LoopWatchdog"]

logger = logging.getLogger(__name__)


class LoopWatchdog:
    """Warns while the event loop is blocked, with the stack of the code that blocks it.

    Code that never awaits, such as `time.sleep()`, a synchronous HTTP call, or a long
    parse, blocks the event loop, and everything else running on it waits. The loop
    beats as it runs, through `beat()`, and says when it should beat next. A daemon
    thread sleeps until the loop is `threshold` seconds late for that beat; if it
    has not beaten by then, the thread logs a WARNING with the stack of the loop's
    thread, once per block. Once the loop beats again, the block is counted, in
    `loop.blocks`, and its seconds in `loop.block_seconds`, and an INFO line says
    how long it lasted. The thread logs for the Run whose context starts it.

    A C call that holds the GIL keeps the thread waiting until it returns. A debugger
    paused on a breakpoint blocks the loop too; a machine asleep does not, as the
    monotonic clock stops meanwhile.
    """

    def __init__(self, threshold: float, *, stats: Stats) -> None:
        """Configure the watching; `loop.blocks` and `loop.block_seconds` start at 0.

        Args:
            threshold: Seconds the loop may be late for a beat before it counts as
                blocked.
            stats: Where the blocks are counted.

        Raises:
            ValueError: `threshold` is not positive.
        """
        if threshold <= 0:
            raise ValueError(f"threshold must be positive, got {threshold}")
        self._threshold = threshold
        self._stats = stats
        self._changed = threading.Condition()
        self._due = 0.0  # when the loop should beat next, on the monotonic clock
        self._reported = False  # whether the block in progress was reported
        self._stopped = False
        self._loop_thread = 0
        self._thread: threading.Thread | None = None
        stats.set("loop.blocks", 0)
        stats.set("loop.block_seconds", 0.0)

    @property
    def threshold(self) -> float:
        """Seconds the loop may be late for a beat before it counts as blocked."""
        return self._threshold

    def start(self, next_beat_in: float) -> None:
        """Start watching the event loop that runs in the calling thread.

        Args:
            next_beat_in: Seconds until the loop's first beat.

        Raises:
            RuntimeError: The watchdog was started before.
        """
        if self._thread is not None:
            raise RuntimeError("a LoopWatchdog watches only once")
        self._loop_thread = threading.get_ident()
        self._due = time.monotonic() + next_beat_in
        run = current_run()
        name = "pernix-watchdog" if run is None else f"pernix-watchdog-{run.run_id[:8]}"
        context = contextvars.copy_context()
        self._thread = threading.Thread(target=context.run, args=(self._watch,), name=name, daemon=True)
        self._thread.start()

    def beat(self, next_beat_in: float) -> None:
        """Record that the loop runs; a block reported before is over. Call it on the loop.

        Args:
            next_beat_in: Seconds until the loop's next beat.
        """
        now = time.monotonic()
        with self._changed:
            late, ended = now - self._due, self._reported
            self._due = now + next_beat_in
            if ended:
                self._reported = False
                self._changed.notify()
        if ended:
            self._count(late)

    def stop(self) -> None:
        """Stop watching, once the thread ended; a block reported before is over. Call it on the loop."""
        now = time.monotonic()
        with self._changed:
            late, ended = now - self._due, self._reported
            self._reported = False
            self._stopped = True
            self._changed.notify()
        if self._thread is not None:
            self._thread.join()
        if ended:
            self._count(late)

    def _count(self, seconds: float) -> None:
        self._stats.inc("loop.blocks")
        self._stats.inc("loop.block_seconds", seconds)
        logger.info("The event loop ran again after being blocked for %.1f s", seconds)

    def _watch(self) -> None:
        # The lock is held while warning, so a beat that ends the block waits, and its line comes after the warning.
        with self._changed:
            while not self._stopped:
                late = time.monotonic() - self._due
                if self._reported:
                    self._changed.wait()
                elif late >= self._threshold:
                    self._reported = True
                    self._warn(late, sys._current_frames().get(self._loop_thread))  # pyright: ignore[reportPrivateUsage]
                else:
                    self._changed.wait(self._threshold - late)

    def _warn(self, late: float, frame: FrameType | None) -> None:
        """Log the block, the stack of the loop's thread as its `stack_info`; `logger.warning()` would take this one's."""
        if not logger.isEnabledFor(logging.WARNING):
            return
        stack = None
        if frame is not None:
            stack = "Stack (most recent call last):\n" + "".join(traceback.format_stack(frame)).rstrip("\n")
        path, line, function, _ = logger.findCaller()
        record = logger.makeRecord(
            logger.name,
            logging.WARNING,
            path,
            line,
            "The event loop has been blocked for %.1f s; nothing in the Run runs until the code below returns or awaits",
            (late,),
            None,
            function,
            None,
            stack,
        )
        logger.handle(record)
