from __future__ import annotations

import csv
import datetime as dt
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

import orjson

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

__all__ = ["CsvFormat", "FeedFormat", "JsonArrayFormat", "JsonLinesFormat", "dump_json", "feed_format"]

logger = logging.getLogger(__name__)


class FeedFormat(Protocol):
    """Lays out the records of one Feed file."""

    def begin(self, out: TextIO) -> None:
        """Write what precedes the first record."""
        ...

    def write(self, out: TextIO, record: dict[str, Any]) -> None:
        """Write one record, a plain dict."""
        ...

    def end(self, out: TextIO) -> None:
        """Write what follows the last record."""
        ...


def dump_json(value: object) -> str:
    """Encode a record as compact JSON; `Decimal`s become strings, to keep their precision.

    Raises:
        TypeError: A value has a type JSON cannot represent, such as `bytes`.
    """
    return orjson.dumps(value, default=_json_default, option=orjson.OPT_NON_STR_KEYS).decode()


class JsonLinesFormat:
    """One JSON object per line."""

    def begin(self, out: TextIO) -> None:
        """Write nothing."""

    def write(self, out: TextIO, record: dict[str, Any]) -> None:
        """Write the record as one line."""
        out.write(dump_json(record) + "\n")

    def end(self, out: TextIO) -> None:
        """Write nothing."""


class JsonArrayFormat:
    """A JSON array written as it grows, one record per line."""

    def __init__(self) -> None:
        """Start an empty array."""
        self._empty = True

    def begin(self, out: TextIO) -> None:
        """Open the array."""
        out.write("[")

    def write(self, out: TextIO, record: dict[str, Any]) -> None:
        """Write the record as the next element, on its own line."""
        line = dump_json(record)  # before writing, so a failure leaves the array well-formed
        out.write(("\n" if self._empty else ",\n") + line)
        self._empty = False

    def end(self, out: TextIO) -> None:
        """Close the array."""
        out.write("]\n" if self._empty else "\n]\n")


class CsvFormat:
    """Comma-separated values with a header row.

    The columns are `fields`, or else the keys of the first record. A record lacking a
    column leaves it empty; keys outside the columns are dropped, with one warning
    per key. `None` is written as an empty cell, booleans and containers as JSON, and
    dates and times in ISO 8601.
    """

    def __init__(self, fields: Sequence[str] | None = None) -> None:
        """Configure the columns.

        Args:
            fields: The columns, in order; `None` takes the keys of the first record.
        """
        self._columns = list(fields) if fields is not None else None
        self._writer: Any = None
        self._warned: set[str] = set()

    def begin(self, out: TextIO) -> None:
        """Write the header row if the columns are known."""
        self._writer = csv.writer(out)
        if self._columns is not None:
            self._writer.writerow(self._columns)

    def write(self, out: TextIO, record: dict[str, Any]) -> None:
        """Write the record as a row, after the header row if this is the first."""
        if self._columns is None:
            self._columns = list(record)
            self._writer.writerow(self._columns)
        for key in sorted(record.keys() - set(self._columns) - self._warned):
            self._warned.add(key)
            logger.warning("Dropping the field %r, which is not a column of the CSV feed", key)
        self._writer.writerow([_cell(record.get(column)) for column in self._columns])

    def end(self, out: TextIO) -> None:
        """Write nothing."""


def feed_format(name: str, *, fields: Sequence[str] | None = None) -> FeedFormat:
    """Create the layout of a Feed format.

    Args:
        name: `"jsonl"`, `"csv"`, or `"json"`.
        fields: The columns of a csv Feed.

    Returns:
        A new layout, for one file.

    Raises:
        ValueError: The format is unknown.
    """
    match name:
        case "jsonl":
            return JsonLinesFormat()
        case "json":
            return JsonArrayFormat()
        case "csv":
            return CsvFormat(fields)
        case _:
            raise ValueError(f"unknown feed format {name!r}; use jsonl, csv, or json")


def _cell(value: object) -> object:
    raw = value  # stays `object` where the match narrows `value` to containers of unknown items
    match value:
        case None:
            return ""
        case bool() | dict() | list() | tuple():
            return dump_json(raw)
        case dt.datetime() | dt.date() | dt.time():
            return value.isoformat()
        case _:
            return value


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"{type(value).__name__} cannot be written as JSON: {value!r}")
