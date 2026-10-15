from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.errors import ResponseTooLarge

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Callable

    from pernix.response import Response

__all__ = ["StreamedResponse"]


class StreamedResponse:
    """A Response whose body is read as it arrives, once, instead of held in memory.

    `HTTPFetcher.stream()` and `Crawler.stream()` give it. The body may be read with `iter_bytes()` or
    written to a file with `save()`, at most `max_bytes` of it.

    Attributes:
        response: The Response without its body: status, headers, URL, redirects,
            and cookies.
        max_bytes: Bytes the body may have at most; `None` for no limit.
    """

    def __init__(
        self,
        response: Response,
        chunks: Callable[[], AsyncIterator[bytes]],
        *,
        max_bytes: int | None,
    ) -> None:
        """Wrap a transfer whose headers have arrived.

        Args:
            response: The Response without its body.
            chunks: Reads the body, chunk by chunk, raising `FetchError` on failure.
            max_bytes: Bytes the body may have at most; `None` for no limit.
        """
        self.response = response
        self.max_bytes = max_bytes
        self._chunks = chunks
        self._read = False

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Read the body as it arrives.

        Yields:
            The body, chunk by chunk.

        Raises:
            RuntimeError: The body was read already.
            pernix.errors.ResponseTooLarge: The body exceeds `max_bytes`.
            pernix.errors.FetchError: The transfer failed.
        """
        if self._read:
            raise RuntimeError("the body of a StreamedResponse can be read only once")
        self._read = True
        size = 0
        async for chunk in self._chunks():
            size += len(chunk)
            if self.max_bytes is not None and size > self.max_bytes:
                raise ResponseTooLarge(f"the body of {self.response.url} exceeds {self.max_bytes} bytes")
            yield chunk

    async def save(self, path: str | os.PathLike[str]) -> int:
        """Write the body to a file, replacing it if it exists.

        Args:
            path: The file.

        Returns:
            The number of bytes written.

        Raises:
            RuntimeError: The body was read already.
            pernix.errors.ResponseTooLarge: The body exceeds `max_bytes`; what was
                written so far is kept.
            pernix.errors.FetchError: The transfer failed.
        """
        target = Path(path)
        file = await asyncio.to_thread(target.open, "wb")
        written = 0
        try:
            async for chunk in self.iter_bytes():
                await asyncio.to_thread(file.write, chunk)
                written += len(chunk)
        finally:
            await asyncio.to_thread(file.close)
        return written
