from __future__ import annotations

import copy
import logging
import queue
from dataclasses import dataclass
from typing import TYPE_CHECKING, override

from pernix.logs.log_context import current_request_id, current_run

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.logs.log_context import RunContext

__all__ = ["LogCounts", "RunQueueHandler"]


@dataclass(slots=True)
class LogCounts:
    """Counters of the records logged for a Run.

    Attributes:
        warning: Records at `WARNING`.
        error: Records at `ERROR`.
        critical: Records at `CRITICAL`.
        dropped: Records dropped because the queue was full.
    """

    warning: int = 0
    error: int = 0
    critical: int = 0
    dropped: int = 0


class RunQueueHandler(logging.Handler):
    """Queues the records logged for one Run, without ever blocking.

    A record belongs to the Run whose `run_context()` is in effect where it is logged;
    a record logged outside every Run belongs to the Run that `owns_unattributed`
    names. Records of other Runs are ignored. Before a record is queued, its message
    is merged with its arguments, its exception is rendered as text, and the Run and
    Request it was logged for are attached as `run_id`, `spider`, and `request_id`, so
    a writer thread can deliver it later and elsewhere. When the queue is full, the
    record is dropped and counted.

    Attributes:
        counts: Counters of the records handled.
    """

    def __init__(
        self,
        records: queue.Queue[logging.LogRecord],
        run: RunContext,
        *,
        owns_unattributed: Callable[[], bool],
    ) -> None:
        """Queue records into `records`.

        Args:
            records: A bounded queue a writer thread reads.
            run: The Run whose records to queue.
            owns_unattributed: Whether this handler also takes the records logged
                outside every Run.
        """
        super().__init__()
        self.counts = LogCounts()
        self._records = records
        self._run = run
        self._owns_unattributed = owns_unattributed
        self._exceptions = logging.Formatter()

    @override
    def emit(self, record: logging.LogRecord) -> None:
        run = current_run()
        if run is None:
            if not self._owns_unattributed():
                return
            run = self._run
        elif run.run_id != self._run.run_id:
            return
        try:
            prepared = self._prepare(record, run)
        except Exception:
            self.handleError(record)
            return
        self._count(record.levelno)
        try:
            self._records.put_nowait(prepared)
        except (queue.Full, queue.ShutDown):
            self.counts.dropped += 1

    def _prepare(self, record: logging.LogRecord, run: RunContext) -> logging.LogRecord:
        prepared = copy.copy(record)
        prepared.msg = record.getMessage()
        prepared.args = None
        if record.exc_info:
            prepared.exc_text = record.exc_text or self._exceptions.formatException(record.exc_info)
        prepared.exc_info = None
        prepared.__dict__.update(run_id=run.run_id, spider=run.spider, request_id=current_request_id())
        return prepared

    def _count(self, level: int) -> None:
        if level >= logging.CRITICAL:
            self.counts.critical += 1
        elif level >= logging.ERROR:
            self.counts.error += 1
        elif level >= logging.WARNING:
            self.counts.warning += 1
