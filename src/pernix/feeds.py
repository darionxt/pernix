from __future__ import annotations

from typing import IO, TYPE_CHECKING, Any, Self

import orjson

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

__all__ = ["JsonLinesFeed"]


class JsonLinesFeed:
    """Writes each Item as one JSON object per line.

    Use it as a context manager: the file is opened on entry and closed on exit.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Configure the feed.

        Args:
            path: The file to write. An existing file is overwritten.
        """
        self._path = path
        self._file: IO[bytes] | None = None

    def __enter__(self) -> Self:
        self._file = open(self._path, "wb")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def write(self, item: Mapping[str, Any]) -> None:
        """Append one Item.

        Args:
            item: The Item to write; it must be JSON-serializable.

        Raises:
            RuntimeError: The feed was not entered with `with`.
            TypeError: The Item is not JSON-serializable.
        """
        if self._file is None:
            raise RuntimeError("JsonLinesFeed must be entered with 'with' before writing")
        self._file.write(orjson.dumps(item) + b"\n")
