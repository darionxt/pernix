from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
from typing import IO, TYPE_CHECKING, Self

from pernix.logs.log_formats import JsonFormatter, TextFormatter, record_extras
from pernix.logs.queue_handler import RunQueueHandler
from pernix.logs.redaction import Redactor

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.logs.log_context import RunContext
    from pernix.logs.queue_handler import LogCounts
    from pernix.settings import LogSettings

__all__ = ["DRAIN_TIMEOUT", "RunLog"]

DRAIN_TIMEOUT = 5.0
"""Seconds closing a RunLog waits for the queued records to be written."""


class RunLog:
    """Delivers the `pernix.*` records of one Run through a bounded queue, off the event loop.

    While open, a `RunQueueHandler` on the `pernix` logger queues each record logged
    for the Run without blocking, and a writer thread masks sensitive values in it
    and delivers it: to the Run's own output, as text or JSON lines, or to the
    handlers of the root logger when the application configured logging (see
    `LogSettings.output`). Meanwhile the `pernix` logger does not propagate, so no
    record is delivered twice. When Pernix writes the output, the queue handler takes
    only records at `log.level` or above, and the `pernix` logger gets the lowest
    level the open Runs need; both are restored once the last open RunLog of the
    process closes. The root logger is never changed.

    Closing waits up to `drain_timeout` for the queued records to be written; records
    a blocked output still holds are then abandoned, with the daemon writer thread.
    Pernix's own output takes no lock that `logging.shutdown()` waits for, so a
    blocked output does not keep the process from exiting.

    Example:
        >>> async with RunLog(settings.section(LogSettings), RunContext("r1", "books")):
        ...     with run_context(RunContext("r1", "books")):
        ...         logging.getLogger("pernix.spiders.books").info("queued, not written here")
    """

    def __init__(
        self,
        settings: LogSettings,
        run: RunContext,
        *,
        secrets: Iterable[str] = (),
        drain_timeout: float = DRAIN_TIMEOUT,
    ) -> None:
        """Configure the log output of a Run.

        Args:
            settings: The log settings of the Run.
            run: The Run whose records to deliver.
            secrets: Values to mask wherever they occur, such as those of `Secret`
                settings.
            drain_timeout: Seconds `close()` waits for the queued records.
        """
        self._settings = settings
        self._run = run
        self._redactor = Redactor(names=settings.redact, secrets=secrets)
        self._drain_timeout = drain_timeout
        self._records: queue.Queue[logging.LogRecord] = queue.Queue(settings.queue_size)
        self.handler = RunQueueHandler(self._records, run, owns_unattributed=lambda: _PERNIX.first_open() is self)
        self._output: _StreamOutput | None = None
        self._writer: threading.Thread | None = None

    @property
    def counts(self) -> LogCounts:
        """Counters of the records logged for the Run, including the dropped ones."""
        return self.handler.counts

    @property
    def level(self) -> int | None:
        """The level the `pernix` logger needs, or `None` when the root handlers decide."""
        if self._output is None:
            return None
        return logging.getLevelNamesMapping()[self._settings.level.upper()]

    def open(self) -> None:
        """Start the writer thread and the queue handler.

        Raises:
            RuntimeError: The RunLog was opened before.
        """
        if self._writer is not None:
            raise RuntimeError("a RunLog opens only once")
        self._output = _own_output(self._settings)
        if self.level is not None:
            self.handler.setLevel(self.level)
        self._writer = threading.Thread(target=self._write, name=f"pernix-log-{self._run.run_id[:8]}", daemon=True)
        self._writer.start()
        _PERNIX.attach(self)

    async def close(self) -> None:
        """Stop queueing records, then wait for the queued ones to be written."""
        if self._writer is None or self._records.is_shutdown:
            return
        _PERNIX.detach(self)
        self._records.shutdown()
        await asyncio.to_thread(self._writer.join, self._drain_timeout)

    async def __aenter__(self) -> Self:
        self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    def _write(self) -> None:
        while True:
            try:
                record = self._records.get()
            except queue.ShutDown:
                return
            try:
                self._redact(record)
                if self._output is None:
                    logging.getLogger().handle(record)
                else:
                    self._output.write(record)
            except Exception:
                self.handler.handleError(record)

    def _redact(self, record: logging.LogRecord) -> None:
        redact = self._redactor.redact
        record.msg = redact(str(record.msg))
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        for name, value in record_extras(record).items():
            record.__dict__[name] = self._redactor.redact_value(value)


class _StreamOutput:
    """Writes formatted records to a stream; only the writer thread calls it.

    Unlike a `logging.StreamHandler`, it holds no lock that `logging.shutdown()`
    would wait for at exit while a blocked write keeps it.
    """

    def __init__(self, stream: IO[str], formatter: logging.Formatter) -> None:
        self._stream = stream
        self._formatter = formatter

    def write(self, record: logging.LogRecord) -> None:
        self._stream.write(self._formatter.format(record) + "\n")
        self._stream.flush()


class _SharedLogger:
    """The `pernix` logger, shared by the RunLogs open in this process."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._open: tuple[RunLog, ...] = ()
        self._saved = (logger.propagate, logger.level)

    def first_open(self) -> RunLog | None:
        """The RunLog opened first among those still open; it takes unattributed records."""
        open_logs = self._open  # replaced, never mutated, so other threads read it safely
        return open_logs[0] if open_logs else None

    def attach(self, run_log: RunLog) -> None:
        with self._lock:
            if not self._open:
                self._saved = (self._logger.propagate, self._logger.level)
            self._open = (*self._open, run_log)
            self._logger.addHandler(run_log.handler)
            self._apply()

    def detach(self, run_log: RunLog) -> None:
        with self._lock:
            self._logger.removeHandler(run_log.handler)
            self._open = tuple(log for log in self._open if log is not run_log)
            if self._open:
                self._apply()
            else:
                propagate, level = self._saved
                self._logger.propagate = propagate
                self._logger.setLevel(level)

    def _apply(self) -> None:
        self._logger.propagate = False
        levels = [log.level for log in self._open if log.level is not None]
        self._logger.setLevel(min(levels) if levels else self._saved[1])


_PERNIX = _SharedLogger(logging.getLogger("pernix"))


def _own_output(settings: LogSettings) -> _StreamOutput | None:
    """Where Pernix writes the Run's output, or `None` to hand records to the root handlers."""
    output = settings.output
    if output == "auto":
        output = "root" if logging.getLogger().handlers else "stderr"
    if output == "root":
        return None
    stream = sys.stdout if output == "stdout" else sys.stderr
    return _StreamOutput(stream, JsonFormatter() if _writes_json(settings.format, stream) else TextFormatter())


def _writes_json(log_format: str, stream: IO[str]) -> bool:
    if log_format != "auto":
        return log_format == "json"
    try:
        return not stream.isatty()
    except (AttributeError, ValueError):  # a replaced or closed stream
        return True
