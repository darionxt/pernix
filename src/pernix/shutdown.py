from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from pernix.component_lifecycle import ComponentLifecycle
    from pernix.engine import Engine

__all__ = ["Shutdown", "StopMode"]

logger = logging.getLogger(__name__)

type StopMode = Literal["drain", "force"]
"""How to stop a Run: `"drain"` finishes the work in flight, `"force"` cancels it."""

_MODES = ("drain", "force")


class Shutdown:
    """Carries out the stop requests of one Run, from opening its components to closing them.

    Between `begin()` and `end()`, a Drain makes the Engine stop claiming Requests
    and starts the grace period; once it is over, the Drain turns into a Force Stop.
    A Force Stop cancels the stage of the Run in progress, opening the components or
    doing the work, if run with `run_stage()`, and limits each step of closing the
    components to `close_timeout` seconds, the step in progress included. A request
    made before `begin()` takes effect then; one made after `end()` does nothing. A
    Drain requested during a Force Stop changes nothing, and a Force Stop requested
    during a Drain escalates it.

    Attributes:
        mode: The stop requested so far, if any.
    """

    def __init__(self, *, grace: float, close_timeout: float | None) -> None:
        """Configure the stopping.

        Args:
            grace: Seconds a Drain may take before it turns into a Force Stop.
            close_timeout: Seconds each component may take to close after a Force
                Stop; `None` sets no limit.
        """
        self.mode: StopMode | None = None
        self._grace = grace
        self._close_timeout = close_timeout
        self._lifecycle: ComponentLifecycle | None = None
        self._engine: Engine | None = None
        self._stage: asyncio.Timeout | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._active = False
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
        if self._active:
            self._apply()

    def begin(self, lifecycle: ComponentLifecycle) -> None:
        """Start carrying out stop requests, applying any made before.

        Args:
            lifecycle: Opens and closes the components of the Run.
        """
        self._lifecycle = lifecycle
        self._active = True
        if self.mode is not None:
            self._apply()

    def attach(self, engine: Engine) -> None:
        """Drain this Engine on a Drain, including one requested before.

        Args:
            engine: The Engine of the Run.
        """
        self._engine = engine
        if self.mode == "drain":
            engine.drain()

    async def run_stage(self, stage: Coroutine[Any, Any, None]) -> bool:
        """Run a stage of the Run under a scope that a Force Stop cancels.

        The stage runs in the calling task, so the context variables a component
        sets while it opens are still set while the Run goes on and when it closes.
        After a Force Stop, a stage does not start.

        Args:
            stage: The stage, such as opening the components.

        Returns:
            Whether a Force Stop cancelled it, or kept it from starting.

        Raises:
            asyncio.CancelledError: The caller was cancelled.
        """
        if self.forced:
            stage.close()
            return True
        scope = asyncio.timeout(None)
        self._stage = scope
        try:
            async with scope:
                await stage
        except TimeoutError:
            if scope.expired():
                return True
            raise
        finally:
            self._stage = None
        return False

    def end(self) -> None:
        """Stop carrying out stop requests; later requests do nothing."""
        self._active = False
        self._ended = True
        if self._timer is not None:
            self._timer.cancel()

    def _apply(self) -> None:
        if self.mode == "drain":
            logger.info("Draining the Run: finishing the work in flight within %s s", self._grace)
            if self._engine is not None:
                self._engine.drain()
            self._timer = asyncio.get_running_loop().call_later(self._grace, self._grace_over)
            return
        if self._timer is not None:
            self._timer.cancel()
        logger.warning("Stopping the Run now: cancelling the work in flight")
        stage = self._stage
        if stage is not None and not stage.expired():
            stage.reschedule(asyncio.get_running_loop().time())
        if self._lifecycle is not None:
            self._lifecycle.limit_closing(self._close_timeout)

    def _grace_over(self) -> None:
        logger.warning("The Drain took longer than shutdown.grace (%s s)", self._grace)
        self.request("force")
