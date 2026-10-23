"""Checking the redirects of a page's navigations before the browser follows them, as the HTTP Fetcher checks its own.

patchright's routes never see a redirect: its driver lets each one go on unseen. A
DevTools session of the page's own, with the Fetch domain on for documents, holds each
request of a document before it is sent, those a redirect leads to included, which name
the request they were redirected from. While a page loads, those of its main frame wait
for the check (`pernix.fetchers.redirects.redirect_check()`), then go on, or fail as
blocked by the client, as a content blocker fails them; the others go on at once.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from patchright.async_api import BrowserContext, CDPSession, Page

    from pernix.fetchers.redirects import RedirectCheck

__all__ = ["NavigationRedirects"]

logger = logging.getLogger(__name__)

_DOCUMENTS = {"patterns": [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]}
"""Hold the requests of documents, before they are sent; no other request is held."""


class NavigationRedirects:
    """Holds each redirect of the navigations of a page's main frame until a check lets it go, from `start()` to `stop()`.

    The check runs in the context of the fetch that started holding them, so it
    fetches as that fetch does, under its Session.

    Attributes:
        refused: What the check raised for the first redirect it refused, if any.
    """

    def __init__(self, devtools: CDPSession, main_frame: str, check: RedirectCheck) -> None:
        """Hold the redirects through a page's DevTools session; `start()` creates it.

        Args:
            devtools: The page's DevTools session.
            main_frame: The id of the page's main frame.
            check: Called with the URL each redirect leads to; what it raises refuses it.
        """
        self._devtools = devtools
        self._main_frame = main_frame
        self._check = check
        self._context = contextvars.copy_context()
        """The fetch's context, a copy of which each check runs in."""
        self._held: dict[str, asyncio.Task[None]] = {}
        """The checks running, by the request each holds."""
        self._sending: set[asyncio.Task[None]] = set()
        self._stopped = False
        self.refused: Exception | None = None

    @classmethod
    async def start(cls, context: BrowserContext, page: Page, check: RedirectCheck) -> NavigationRedirects:
        """Start holding the redirects of a page's main frame, before it navigates.

        Args:
            context: The page's context.
            page: The page.
            check: Called with the URL each redirect leads to, in the context of
                the current task; what it raises refuses the redirect.

        Returns:
            What holds them, until stopped.
        """
        devtools = await context.new_cdp_session(page)
        try:
            tree = cast("dict[str, Any]", await devtools.send("Page.getFrameTree"))  # pyright: ignore[reportUnknownMemberType]
            holding = cls(devtools, str(tree["frameTree"]["frame"]["id"]), check)
            devtools.on("Fetch.requestPaused", holding._paused)
            await _send(devtools, "Fetch.enable", _DOCUMENTS)
        except BaseException:
            await _detach(devtools)
            raise
        return holding

    async def stop(self) -> None:
        """Stop holding redirects, and detach the session.

        A request still held once the session is detached would go on unchecked,
        so the checks still running are cancelled first, each failing its request.
        """
        self._stopped = True
        checks = list(self._held.values())
        for running in checks:
            running.cancel()
        await asyncio.gather(*checks, *self._sending, return_exceptions=True)
        await _detach(self._devtools)  # attached, it could keep the page from closing, which then hangs the fetch

    def _paused(self, event: dict[str, Any]) -> None:
        """Check a request the browser holds if a redirect of the main frame leads to it; let any other go on."""
        held = str(event["requestId"])
        if self._stopped or "redirectedRequestId" not in event or event.get("frameId") != self._main_frame:
            self._start(self._go_on(held))
            return
        url = str(event["request"]["url"])
        checking = self._checked(held, url)
        self._held[held] = asyncio.get_running_loop().create_task(checking, context=self._context.copy())

    async def _checked(self, held: str, url: str) -> None:
        """Check the URL a redirect leads to, then let its request go on, or fail it."""
        try:
            await self._check(url)
        except asyncio.CancelledError:
            await self._fail(held, "Aborted")
            raise
        except Exception as exc:
            if self.refused is None:
                self.refused = exc
            logger.debug("A redirect of a page to %s was refused: %r", url, exc)
            await self._fail(held, "BlockedByClient")
        else:
            await self._go_on(held)
        finally:
            self._held.pop(held, None)

    async def _go_on(self, held: str) -> None:
        try:
            await _send(self._devtools, "Fetch.continueRequest", {"requestId": held})
        except Exception:  # the page went away, with its requests
            logger.debug("Letting a request of a page go on failed", exc_info=True)

    async def _fail(self, held: str, reason: str) -> None:
        try:
            await _send(self._devtools, "Fetch.failRequest", {"requestId": held, "errorReason": reason})
        except Exception:  # the page went away, with its requests
            logger.debug("Failing a request of a page failed", exc_info=True)

    def _start(self, sending: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine sending a command in a task of its own, kept until it ends."""
        task = asyncio.get_running_loop().create_task(sending)
        self._sending.add(task)
        task.add_done_callback(self._sending.discard)


async def _send(devtools: CDPSession, method: str, params: dict[str, Any]) -> None:
    """Send a command of the DevTools protocol."""
    await devtools.send(method, params)  # pyright: ignore[reportUnknownMemberType]  # untyped


async def _detach(devtools: CDPSession) -> None:
    try:
        await devtools.detach()
    except Exception:  # the page went away, with its session
        logger.debug("Detaching the DevTools session of a page failed", exc_info=True)
