from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from pernix.errors import FeedError
from pernix.feeds.feed_formats import feed_format
from pernix.items.item_adapter import item_matcher, item_to_dict
from pernix.settings import FeedOptions
from pernix.settings.core_settings import FEED_FORMATS

if TYPE_CHECKING:
    from pernix.feeds.feed_formats import FeedFormat

__all__ = ["Feed", "render_path"]


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
    """Writes the Items it accepts to one file, which appears complete or not at all.

    `open()` creates the missing parent directories and a hidden temporary file next
    to the target. `close()` ends the format, such as the closing bracket of a JSON
    array, syncs the file to disk, and renames it over the target, replacing any
    existing file. A Feed is closed even when its Run fails, so the Items written so
    far are kept.

    Attributes:
        path: The file the Feed produces.
    """

    def __init__(self, path: str | os.PathLike[str], options: FeedOptions | None = None) -> None:
        """Configure the Feed.

        Args:
            path: The target file, with its placeholders already filled in.
            options: The format, fields, accepted Items, and encoding; the format
                defaults to the one the file extension implies.

        Raises:
            ValueError: No format is given and the extension implies none.
            TypeError: `options.accepts` is neither types nor a function.
        """
        options = options or FeedOptions()
        self.path = Path(path)
        name = options.format or FEED_FORMATS.get(self.path.suffix.lower())
        if name is None:
            raise ValueError(f"cannot infer the feed format of {self.path}; set format to jsonl, csv, or json")
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
        """Create the temporary file and begin the format."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._partial = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.part")
        self._file = open(self._partial, "x", encoding=self._encoding, newline="")  # noqa: SIM115  # closed in close()
        self._format.begin(self._file)

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
        except Exception as exc:
            raise FeedError(f"cannot write an Item to {self.path}: {exc}") from exc

    def close(self) -> None:
        """End the format and move the finished file into place; closing again does nothing."""
        file, partial = self._file, self._partial
        if file is None or partial is None:
            return
        self._file = None
        try:
            self._format.end(file)
            file.flush()
            os.fsync(file.fileno())
        finally:
            file.close()
        partial.replace(self.path)

    def __repr__(self) -> str:
        return f"<Feed {self.path}>"
