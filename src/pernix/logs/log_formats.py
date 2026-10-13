from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from typing import Any, cast, override

import orjson

__all__ = ["CONTEXT_ATTRIBUTES", "JsonFormatter", "TextFormatter", "record_extras"]

CONTEXT_ATTRIBUTES = ("run_id", "spider", "request_id")
"""The attributes Pernix attaches to the records it queues."""

_RECORD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def record_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Return the fields a record carries beyond the standard and context attributes.

    Args:
        record: A log record, such as one logged with `extra=`.

    Returns:
        Attribute name to value.
    """
    return {
        name: value
        for name, value in record.__dict__.items()
        if name not in _RECORD_ATTRIBUTES and name not in CONTEXT_ATTRIBUTES
    }


class TextFormatter(logging.Formatter):
    """Formats records as human-readable lines.

    A line holds the local time, the level, the logger, the id of the Request being
    processed if any, and the message, followed by the exception and the stack. A
    record whose extra fields are a `stats` mapping and `final=True`, as the summary
    at the end of a Run, is followed by its stats, one per line.

    Example:
        `2026-10-13 16:04:05.123 INFO     [pernix.engine #17] Retrying <Request GET …>`
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        request_id = cast("str | None", getattr(record, "request_id", None))
        source = record.name if request_id is None else f"{record.name} #{request_id}"
        time = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        lines = [f"{time}.{int(record.msecs):03d} {record.levelname:<8} [{source}] {record.message}"]
        exception = _exception_text(self, record)
        if exception:
            lines.append(exception)
        if record.stack_info:
            lines.append(self.formatStack(record.stack_info))
        stats: object = getattr(record, "stats", None)
        if getattr(record, "final", False) and isinstance(stats, Mapping):
            lines.extend(f"  {name}: {value}" for name, value in cast("Mapping[str, object]", stats).items())
        return "\n".join(lines)


class JsonFormatter(logging.Formatter):
    """Formats each record as one JSON object, for log collectors.

    The fields are `time` (UTC, ISO 8601), `level`, `logger`, `message`, `run_id`,
    `spider`, and `request_id` (`null` outside a Request), then `exception` and `stack`
    when present, and every extra field of the record. Extra fields never replace
    these; values JSON cannot represent are written as strings.
    """

    @override
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "time": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": getattr(record, "run_id", None),
            "spider": getattr(record, "spider", None),
            "request_id": getattr(record, "request_id", None),
        }
        exception = _exception_text(self, record)
        if exception:
            fields["exception"] = exception
        if record.stack_info:
            fields["stack"] = self.formatStack(record.stack_info)
        extras = {name: value for name, value in record_extras(record).items() if name not in fields}
        try:
            return _dumps({**fields, **extras})
        except TypeError:  # an extra JSON cannot hold, such as an integer beyond 64 bits
            return _dumps({**fields, **{name: repr(value) for name, value in extras.items()}})


def _exception_text(formatter: logging.Formatter, record: logging.LogRecord) -> str:
    if record.exc_info and not record.exc_text:
        record.exc_text = formatter.formatException(record.exc_info)
    return record.exc_text or ""


def _dumps(payload: Mapping[str, Any]) -> str:
    return orjson.dumps(payload, default=str, option=orjson.OPT_NON_STR_KEYS).decode()
