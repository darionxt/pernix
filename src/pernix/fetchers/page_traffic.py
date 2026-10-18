"""What a rendered page loads: the resource types it may not load, and the responses captured from it."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pernix.browser import RESOURCE_TYPES, CapturedResponse
from pernix.browser.url_globs import glob_pattern
from pernix.headers import Headers

if TYPE_CHECKING:
    from collections.abc import Iterable

    from patchright.async_api import Page, Route
    from patchright.async_api import Response as PageResponse

__all__ = ["ResourceBlocker", "ResponseCapture", "body_headers", "captured_response"]

logger = logging.getLogger(__name__)

_NAMED_TYPES = RESOURCE_TYPES | {"document", "websocket"}
"""The resource types blocking names; `other` covers the rest, such as `ping` for beacons."""

_CAPTURED_TYPES = frozenset({"xhr", "fetch"})

_ENCODING_HEADERS = frozenset({"content-encoding", "content-length"})
"""Headers that describe the body as it was sent, not as the browser decoded it."""


class ResourceBlocker:
    """A route handler that aborts the requests of resource types a page may not load, as a content blocker would."""

    def __init__(self, types: Iterable[str]) -> None:
        """Block some resource types.

        Args:
            types: The resource types, as `pernix.browser.ResourceType`s.
        """
        self._types = frozenset(types)
        self._others = "other" in self._types

    def blocks(self, resource_type: str) -> bool:
        """Whether a request of a resource type is blocked; the types blocking does not name count as `other`.

        Args:
            resource_type: The type the browser reports, such as `image` or `ping`.

        Returns:
            Whether to abort it.
        """
        return resource_type in self._types or (self._others and resource_type not in _NAMED_TYPES)

    async def __call__(self, route: Route) -> None:
        """Abort a request whose type is blocked, and pass the others on to the next handler.

        Args:
            route: The request, held by the browser.
        """
        if self.blocks(route.request.resource_type):
            await route.abort("blockedbyclient")
        else:
            await route.fallback()


class ResponseCapture:
    """Captures the XHR and fetch responses of a page whose URLs match some globs, with their bodies.

    Each body is read as its response arrives, in a task of its own, so that it is
    still there once the actions are done.
    """

    def __init__(self, page: Page, patterns: Iterable[str]) -> None:
        """Start capturing.

        Args:
            page: The page.
            patterns: URL globs, any of which a URL must match.
        """
        self._page = page
        self._patterns = [glob_pattern(pattern) for pattern in patterns]
        self._reads: list[asyncio.Task[CapturedResponse | None]] = []
        self._listening = True
        page.on("response", self._on_response)

    async def responses(self, *, within: float) -> tuple[CapturedResponse, ...]:
        """Stop capturing, and give the responses captured whose bodies arrived in time.

        Args:
            within: Seconds to wait for bodies still arriving; those that did not
                arrive by then are left out.

        Returns:
            The responses, in the order they arrived.
        """
        self._stop_listening()
        if not self._reads:
            return ()
        done, pending = await asyncio.wait(self._reads, timeout=within)
        for read in pending:
            logger.debug("Leaving out a captured response whose body did not arrive within %s s", within)
            read.cancel()
        arrived = (read.result() for read in self._reads if read in done and not read.cancelled())
        return tuple(captured for captured in arrived if captured is not None)

    def stop(self) -> None:
        """Stop capturing, and stop reading the bodies still arriving."""
        self._stop_listening()
        for read in self._reads:
            read.cancel()

    def _stop_listening(self) -> None:
        if self._listening:  # removing a listener twice fails
            self._listening = False
            self._page.remove_listener("response", self._on_response)

    def _on_response(self, response: PageResponse) -> None:
        if response.request.resource_type in _CAPTURED_TYPES and any(
            pattern.fullmatch(response.url) for pattern in self._patterns
        ):
            self._reads.append(asyncio.create_task(_read(response)))


async def captured_response(response: PageResponse, body: bytes | None = None) -> CapturedResponse:
    """A response as the browser received it.

    Args:
        response: The response.
        body: Its body, if read already.

    Returns:
        The response, its body decoded, without the headers describing the encoded
        one.
    """
    headers = Headers((header["name"], header["value"]) for header in await response.headers_array())
    return CapturedResponse(
        url=response.url,
        status=response.status,
        headers=body_headers(headers),
        body=await response.body() if body is None else body,
    )


def body_headers(headers: Headers, *, re_encoded: bool = False) -> Headers:
    """Headers without `Content-Encoding` and `Content-Length` if they no longer describe the body.

    The browser decodes every encoding it does not reject, so the body it gives is
    no longer the one those headers describe.

    Args:
        headers: The headers of a response.
        re_encoded: The body is not the one received at all, such as a rendered
            DOM.

    Returns:
        The headers of the body given.
    """
    encodings = headers.get_all("Content-Encoding")
    if not re_encoded and not any(value.strip().lower() not in ("", "identity") for value in encodings):
        return headers
    return Headers((name, value) for name, value in headers if name.lower() not in _ENCODING_HEADERS)


async def _read(response: PageResponse) -> CapturedResponse | None:
    try:
        return await captured_response(response)
    except Exception:  # a redirect, or a page closed meanwhile, has no body to give
        logger.debug("Leaving out the captured response of %s, whose body cannot be read", response.url, exc_info=True)
        return None
