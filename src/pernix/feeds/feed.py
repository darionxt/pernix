from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from pernix.errors import FeedError
from pernix.feeds.feed_formats import feed_format
from pernix.items.item_adapter import item_matcher, item_to_dict
from pernix.settings import FeedOptions
from pernix.settings.core_settings import FEED_FORMATS

if TYPE_CHECKING:
    from pernix.feeds.feed_formats import FeedFormat

__all__ = ["Feed", "FeedWriting", "render_path"]

logger = logging.getLogger(__name__)

type FeedWriting = Literal["atomic", "in_place", "continued"]
"""How a Feed writes its file: see `Feed`."""


def _ascii_compatible(encoding: str) -> bool:
    """Whether ASCII characters are their own single bytes in an encoding, as recovering a file in place needs."""
    sample = '\n\r",[]{}'
    try:
        return sample.encode(encoding) == sample.encode("ascii")
    except (LookupError, UnicodeError):
        return False


def render_path(template: str, *, name: str, run_id: str, started: dt.datetime) -> Path:
    """Fill in the placeholders of a Feed path template.

    Args:
        template: A path that may contain `{name}`, `{run_id}`, and `{time}`.
        name: The Spider's name.
        run_id: The Run's id.
        started: When the Run started; `{time}` is it in UTC, such as `20261013T080405Z`.

    Returns:
        The path.
    """
    time = started.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return Path(template.format(name=name, run_id=run_id, time=time))


class Feed:
    """Writes the Items it accepts to one file.

    By default (`writing="atomic"`) the file appears complete or not at all:
    `open()` creates the missing parent directories and a hidden temporary file
    next to the target, and `close()` ends the format, such as the closing bracket
    of a JSON array, syncs the file to disk, and renames it over the target,
    replacing any existing file.

    A Run that can be resumed writes its Feeds in place instead, so that what it
    wrote survives its process: the file is the target from the start, and each
    Item is flushed to the operating system as it is written, though not synced to
    disk, which `close()` does. With `writing="in_place"` an existing file is
    replaced as it is opened; with `"continued"`, it is continued, as a resumed Run
    continues the Feeds of the processes before. A process killed while writing
    may leave a partial last record, which is cut off first, with a warning; a
    JSON array ended before is reopened, and is complete only once closed again;
    a CSV file keeps its header row, whose columns it then takes. Writing in place
    needs an encoding where ASCII characters are single bytes, such as UTF-8.

    A Feed is closed even when its Run fails, so the Items written so far are kept.

    Attributes:
        path: The file the Feed produces.
        format: The name of its format: `jsonl`, `csv`, or `json`.
    """

    def __init__(
        self, path: str | os.PathLike[str], options: FeedOptions | None = None, *, writing: FeedWriting = "atomic"
    ) -> None:
        """Configure the Feed.

        Args:
            path: The target file, with its placeholders already filled in.
            options: The format, fields, accepted Items, and encoding; the format
                defaults to the one the file extension implies.
            writing: `"atomic"` to write a temporary file renamed over the target
                once closed; `"in_place"` to write the target itself, replacing it;
                `"continued"` to continue it.

        Raises:
            ValueError: No format is given and the extension implies none, or the
                file is written in place in an encoding where ASCII characters are
                not single bytes.
            TypeError: `options.accepts` is neither types nor a function.
        """
        options = options or FeedOptions()
        self.path = Path(path)
        name = options.format or FEED_FORMATS.get(self.path.suffix.lower())
        if name is None:
            raise ValueError(f"cannot infer the feed format of {self.path}; set format to jsonl, csv, or json")
        if writing != "atomic" and not _ascii_compatible(options.encoding):
            raise ValueError(
                f"the feed {self.path} cannot be written in place, as a resumable Run writes it, "
                f"in {options.encoding}; use an encoding such as utf-8"
            )
        self.format: str = name
        self._writing: FeedWriting = writing
        self._format: FeedFormat = feed_format(name, fields=options.fields)
        self._fields = options.fields if name != "csv" else None  # csv selects its columns itself
        self._encoding = options.encoding
        self._accepts = item_matcher(options.accepts)
        self._partial: Path | None = None
        self._file: TextIO | None = None

    def wants(self, item: object) -> bool:
        """Tell whether the Feed writes this Item."""
        return self._accepts(item)

    def open(self) -> None:
        """Create the file, or the temporary one, and begin the format, or continue the file.

        Raises:
            pernix.errors.FeedError: A file to continue cannot be continued, such as
                a CSV file whose columns are not the fields of the Feed.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._writing == "atomic":
            self._partial = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.part")
            self._file = open(self._partial, "x", encoding=self._encoding, newline="")  # noqa: SIM115  # closed in close()
            self._format.begin(self._file)
            return
        kept = self._recover() if self._writing == "continued" else 0
        self._file = open(self.path, "a" if kept else "w", encoding=self._encoding, newline="")  # noqa: SIM115
        if not kept:
            self._format.begin(self._file)
            self._file.flush()

    def _recover(self) -> int:
        """Cut the file back to its whole records, for the format to continue it; return how many bytes are kept."""
        try:
            with open(self.path, "rb") as file:
                size = os.fstat(file.fileno()).st_size
                kept = self._format.recover(file, self._encoding) if size else 0
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise FeedError(f"cannot continue the feed {self.path}: {exc}") from exc
        if kept < size:
            logger.warning("Cut %d bytes of a partial record off the end of the feed %s", size - kept, self.path)
            os.truncate(self.path, kept)
        return kept

    def write(self, item: object) -> None:
        """Append one Item.

        Args:
            item: An Item; it is converted with `pernix.items.item_to_dict()`.

        Raises:
            RuntimeError: The Feed is not open.
            pernix.errors.FeedError: The Item cannot be written, such as when a value
                has no form in the format, or writing to the file fails.
        """
        if self._file is None:
            raise RuntimeError(f"the feed {self.path} is not open")
        try:
            record: dict[str, Any] = item_to_dict(item)
            if self._fields is not None:
                record = {field: record[field] for field in self._fields if field in record}
            self._format.write(self._file, record)
            if self._writing != "atomic":
                self._file.flush()  # before the Item counts as stored, and its Request is acknowledged
        except Exception as exc:
            raise FeedError(f"cannot write an Item to {self.path}: {exc}") from exc

    def close(self) -> None:
        """End the format, sync the file, and move a temporary one into place; closing again does nothing."""
        file, partial = self._file, self._partial
        if file is None:
            return
        self._file = None
        try:
            self._format.end(file)
            file.flush()
            os.fsync(file.fileno())
        finally:
            file.close()
        if partial is not None:
            partial.replace(self.path)

    def __repr__(self) -> str:
        return f"<Feed {self.path}>"
