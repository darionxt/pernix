"""The processes of a launched browser: the browser's own, those it starts, and the driver that started it."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psutil

__all__ = ["BrowserProcesses"]


@dataclass(frozen=True, slots=True)
class BrowserProcesses:
    """The processes of one browser, found by the process id of the browser's own, for its memory and its end.

    The browser process starts the others, such as those of its renderers and its
    GPU, and patchright's driver started it. A driver that dies leaves the browser
    running, so the processes are killed once the browser is done with.

    Attributes:
        browser: The id of the browser's own process.
        created: When it was created, as the system tells, so that a process that
            took its id after it ended is never taken for it.
        driver: The id of the driver's process, which started it, if known.
    """

    browser: int
    created: float
    driver: int | None

    @classmethod
    def of(cls, pid: int) -> BrowserProcesses | None:
        """Find the processes of the browser whose own process has an id.

        Args:
            pid: The id of the browser's own process.

        Returns:
            The processes; `None` if the process is gone, or psutil, which the
            `browser` extra installs, is not.
        """
        try:
            import psutil  # an optional dependency, installed with the browser extra
        except ImportError:
            return None
        try:
            process = psutil.Process(pid)
            parent = process.parent()
            return cls(pid, process.create_time(), None if parent is None else parent.pid)
        except psutil.Error:
            return None

    def memory(self) -> int:
        """Measure the memory of the browser: the resident set sizes of its process and those it started, summed.

        Returns:
            Bytes; 0 once the browser's process is gone.
        """
        import psutil  # an optional dependency, installed with the browser extra

        total = 0
        for process in self._tree():
            with contextlib.suppress(psutil.Error):  # one that ended meanwhile takes no memory
                total += process.memory_info().rss
        return total

    def driver_alive(self) -> bool:
        """Whether the driver's process still runs; `True` if it is not known."""
        if self.driver is None:
            return True
        import psutil  # an optional dependency, installed with the browser extra

        try:
            return psutil.Process(self.driver).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def kill(self) -> None:
        """Kill the browser's process and those it started, unless they ended already."""
        import psutil  # an optional dependency, installed with the browser extra

        for process in self._tree():
            with contextlib.suppress(psutil.Error):
                process.kill()

    def kill_driver(self) -> None:
        """Kill the driver's process, as when it stopped answering."""
        if self.driver is None:
            return
        import psutil  # an optional dependency, installed with the browser extra

        with contextlib.suppress(psutil.Error):
            psutil.Process(self.driver).kill()

    def _tree(self) -> list[psutil.Process]:
        """The browser's process, first, then those it started; none once it is gone or another took its id."""
        import psutil  # an optional dependency, installed with the browser extra

        try:
            process = psutil.Process(self.browser)
            if process.create_time() != self.created:
                return []
            return [process, *process.children(recursive=True)]
        except psutil.Error:
            return []
