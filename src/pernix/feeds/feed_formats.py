from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
from decimal import Decimal
from typing import TYPE_CHECKING, Any, BinaryIO, Protocol

import orjson

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

__all__ = ["CsvFormat", "FeedFormat", "JsonArrayFormat", "JsonLinesFormat", "dump_json", "feed_format"]

logger = logging.getLogger(__name__)


class FeedFormat(Protocol):
    """Lays out the records of one Feed file.

    A file is begun, written, and ended; or, to continue one written before, as
    when a Run is resumed, recovered, then written and ended.
    """

    def begin(self, out: TextIO) -> None:
        """Write what precedes the first record."""
        ...

    def write(self, out: TextIO, record: dict[str, Any]) -> None:
        """Write one record, a plain dict."""
        ...

    def end(self, out: TextIO) -> None:
        """Write what follows the last record."""
        ...

    def recover(self, file: BinaryIO, encoding: str) -> int:
        """Take up a file this format wrote before, which may end in a partial record or be ended.

        The format then continues after what it keeps, as if it had written it; with
        nothing kept, the file is begun again.

        Args:
            file: The file, open for reading bytes.
            encoding: Its text encoding, one where ASCII characters are single
                bytes.

        Returns:
            How many of its bytes to keep: its whole records, without their end.

        Raises:
            ValueError: The file cannot be continued as this format is configured,
                such as a CSV file with other columns.
        """
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

    def recover(self, file: BinaryIO, encoding: str) -> int:
        """Keep the lines that end; a partial last one is a record cut short."""
        return _line_start(file, file.seek(0, os.SEEK_END))


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

    def recover(self, file: BinaryIO, encoding: str) -> int:
        """Keep the array up to its last whole element, without its closing bracket, if any.

        An element is whole once its line, decoded, parses, its separating comma
        aside.
        """
        end = file.seek(0, os.SEEK_END)
        while end > 0:
            start = _line_start(file, end - 1)
            file.seek(start)
            line = file.read(end - start)
            text = line.strip()
            if text in (b"[", b"[]"):  # an array without elements, ended or not
                self._empty = True
                return start + line.index(b"[") + 1
            element = text.removesuffix(b",")
            if element not in (b"", b"]"):
                try:
                    orjson.loads(element.decode(encoding))
                except (UnicodeDecodeError, orjson.JSONDecodeError):
                    pass  # a record cut short
                else:
                    self._empty = False
                    return start + len(line) - len(line.lstrip()) + len(element)
            end = start
        return 0


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
        if self._writer is None:  # continuing a file, whose header row is written
            self._writer = csv.writer(out)
        if self._columns is None:
            self._columns = list(record)
            self._writer.writerow(self._columns)
        for key in sorted(record.keys() - set(self._columns) - self._warned):
            self._warned.add(key)
            logger.warning("Dropping the field %r, which is not a column of the CSV feed", key)
        self._writer.writerow([_cell(record.get(column)) for column in self._columns])

    def end(self, out: TextIO) -> None:
        """Write nothing."""

    def recover(self, file: BinaryIO, encoding: str) -> int:
        """Keep the rows that end, and take the columns from the header row.

        A row ends at a line break outside quotes, so a row whose quoted cells hold
        line breaks is kept whole or not at all.

        Raises:
            ValueError: The header row is not the columns `fields` names.
        """
        rows = _row_ends(file)
        if not rows:
            return 0
        file.seek(0)
        [header] = csv.reader(io.StringIO(file.read(rows[0]).decode(encoding), newline=""))
        if self._columns is not None and self._columns != header:
            raise ValueError(f"its columns are {header}, not the fields {self._columns}")
        self._columns = header
        return rows[-1]


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


_CHUNK = 1 << 16
"""Bytes read at once when looking for where records end."""


def _line_start(file: BinaryIO, end: int) -> int:
    """Return where the line holding the byte before `end` begins: just after the line break before it, or 0."""
    position = end
    while position > 0:
        start = max(0, position - _CHUNK)
        file.seek(start)
        found = file.read(position - start).rfind(b"\n")
        if found >= 0:
            return start + found + 1
        position = start
    return 0


def _row_ends(file: BinaryIO) -> list[int]:
    """Return where the first and the last CSV rows that end do: after a line break outside quotes.

    Quotes are counted from the start, as a doubled quote inside a quoted cell
    leaves the count even; only the first and the last rows are looked for.
    """
    size = file.seek(0, os.SEEK_END)
    first: int | None = None
    quotes_before: list[int] = []  # the quotes before each chunk
    quotes = 0
    file.seek(0)
    for start in range(0, size, _CHUNK):
        chunk = file.read(_CHUNK)
        quotes_before.append(quotes)
        if first is None:
            first = _first_break_outside_quotes(chunk, quotes, start)
        quotes += chunk.count(b'"')
    if first is None:
        return []
    for index in reversed(range(len(quotes_before))):
        start = index * _CHUNK
        file.seek(start)
        chunk = file.read(_CHUNK)
        quotes = quotes_before[index] + chunk.count(b'"')  # before the end of the chunk, then before each break
        end = len(chunk)
        while (found := chunk.rfind(b"\n", 0, end)) >= 0:
            quotes -= chunk.count(b'"', found, end)
            if quotes % 2 == 0:
                return [first, start + found + 1]
            end = found
    return [first, first]


def _first_break_outside_quotes(chunk: bytes, quotes: int, start: int) -> int | None:
    """Return where the first line break outside quotes in a chunk ends, after the quotes before it, if there is one."""
    position = 0
    while (found := chunk.find(b"\n", position)) >= 0:
        quotes += chunk.count(b'"', position, found)
        if quotes % 2 == 0:
            return start + found + 1
        position = found + 1
    return None


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
