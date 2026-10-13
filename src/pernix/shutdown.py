from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pernix.engine import Engine

__all__ = ["Shutdown", "StopMode"]

logger = logging.getLogger(__name__)

type StopMode = Literal["drain", "force"]
"""How to stop a Run: `"drain"` finishes the work in flight, `"force"` cancels it."""

_MODES = ("drain", "force")


class Shutdown:
    """Carries out the stop requests of one Run.

    A Drain makes the Engine stop claiming Requests and starts the grace period; once
    it is over, the Drain turns into a Force Stop. A Force Stop cancels the Run's
    work: the Requests in flight and the Item processing. A request made before the
    work starts takes effect when it does; one made after it ends does nothing. A
    Drain requested during a Force Stop changes nothing, and a Force Stop requested
    during a Drain escalates it.

    Attributes:
        mode: The stop requested so far, if any.
    """

    def __init__(self, *, grace: float) -> None:
        """Configure the stopping.

        Args:
            grace: Seconds a Drain may take before it turns into a Force Stop.
        """
        self.mode: StopMode | None = None
        self._grace = grace
        self._engine: Engine | None = None
        self._work: asyncio.Task[None] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._ended = False

    @property
    def forced(self) -> bool:
        """Whether a Force Stop was requested."""
        return self.mode == "force"

    def request(self, mode: StopMode) -> None:
        """Ask the Run to stop.

        Args:
            mode: `"drain"` or `"force"`.

        Raises:
            ValueError: `mode` is neither.
        """
        if mode not in _MODES:
            raise ValueError(f"the stop mode must be 'drain' or 'force', got {mode!r}")
        if self._ended or self.mode == mode or self.forced:
            return
        self.mode = mode
        self._apply()

    def start(self, engine: Engine, work: asyncio.Task[None]) -> None:
        """Begin watching the work of the Run, applying any stop requested before.

        Args:
            engine: The Engine to drain.
            work: The task doing the Run's work, to cancel on a Force Stop.
        """
        self._engine, self._work = engine, work
        self._apply()

    def end(self) -> None:
        """Stop watching the Run; later requests do nothing."""
        self._ended = True
        if self._timer is not None:
            self._timer.cancel()

    def _apply(self) -> None:
        engine, work = self._engine, self._work
        if engine is None or work is None or self.mode is None:
            return
        if self.mode == "drain":
            logger.info("Draining the Run: finishing the work in flight within %s s", self._grace)
            engine.drain()
            self._timer = asyncio.get_running_loop().call_later(self._grace, self._grace_over)
            return
        if self._timer is not None:
            self._timer.cancel()
        logger.warning("Stopping the Run now: cancelling the work in flight")
        work.cancel()

    def _grace_over(self) -> None:
        logger.warning("The Drain took longer than shutdown.grace (%s s)", self._grace)
        self.request("force")
