"""Presenting a locale and a time zone in a page's `Intl`, and in its frames and web workers, without moving a header."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, CDPSession, Page

__all__ = ["present_intl"]

logger = logging.getLogger(__name__)

type Route = tuple[str, ...]
"""The DevTools sessions that lead to a target attached through a page's own session, outermost first."""

_RELATED = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": False,
    "filter": [{"type": "iframe"}, {"type": "service_worker"}],
}
"""Attach to the cross-site frames and the service workers of a target, each paused until told to run.

Not flattened, as a patchright `CDPSession` can reach the session of a target
attached to it only through `Target.sendMessageToTarget`. Dedicated workers run in
the process of the frame that starts them, so they are left alone.
"""

_ALREADY_OVERRIDDEN = "Another locale override is already in effect"
"""What Chrome answers a target whose process already holds an override, which its targets share."""


async def present_intl(context: BrowserContext, page: Page, *, locale: str | None, timezone: str | None) -> None:
    """Make a page, before it loads anything, present a locale and a time zone in `Intl`, with its frames and workers.

    Chrome's `Emulation.setLocaleOverride` and `Emulation.setTimezoneOverride` hold
    for the renderer process of the target they are sent to, so for the page's
    same-site frames and the dedicated and shared workers that run in it. A
    DevTools session of the page's own sets the locale, and attaches to the page's
    cross-site frames, at any depth, and its service workers, which may run in
    processes of their own, pausing each until both are set; it lets each run
    whatever failed. patchright's `locale` option would set the locale too, but it
    moves `Accept-Language` after `User-Agent`. The page's time zone, and that of
    its frames, is patchright's `timezone_id`; a service worker that started for an
    earlier page of the context needs its own.

    The overrides last as long as the session, which closes with the page. A page
    the page opens in another process, as with `noopener`, presents the machine's
    locale; a service worker already running when the page loads takes the locale
    and the time zone once attached, as the page navigates.

    Args:
        context: The page's context.
        page: The page, not yet navigated.
        locale: The locale, a language tag such as `de-DE`; `None` for the
            machine's.
        timezone: The time zone, an IANA name, which the context presents already;
            `None` for the machine's.
    """
    devtools = await context.new_cdp_session(page)
    related = _RelatedTargets(devtools, locale=locale, timezone=timezone)
    devtools.on("Target.attachedToTarget", related.attached)
    devtools.on("Target.receivedMessageFromTarget", related.received)
    if locale is not None:
        try:
            await _send(devtools, "Emulation.setLocaleOverride", {"locale": locale})
        except Exception as exc:  # the page's process holds the override already
            if _ALREADY_OVERRIDDEN not in str(exc):
                raise
    await _send(devtools, "Target.setAutoAttach", _RELATED)


class _RelatedTargets:
    """Sets the locale and the time zone of each target attached through a page's DevTools session, then lets it run."""

    def __init__(self, devtools: CDPSession, *, locale: str | None, timezone: str | None) -> None:
        """Serve the targets attached through a session.

        Args:
            devtools: The page's DevTools session.
            locale: The locale to set, if any.
            timezone: The time zone to set, if any.
        """
        self._devtools = devtools
        self._overrides = [
            *([] if locale is None else [("Emulation.setLocaleOverride", {"locale": locale})]),
            *([] if timezone is None else [("Emulation.setTimezoneOverride", {"timezoneId": timezone})]),
        ]
        self._ids = itertools.count(1)
        self._setting: set[asyncio.Task[None]] = set()

    def attached(self, event: dict[str, Any], parent: Route = ()) -> None:
        """Set the overrides of a target just attached, in a task of its own.

        Args:
            event: The `Target.attachedToTarget` event.
            parent: The sessions leading to the target it was attached through;
                none for the page.
        """
        route = (*parent, str(event["sessionId"]))
        frame = event["targetInfo"]["type"] == "iframe"
        setting = asyncio.get_running_loop().create_task(self._set(route, frame=frame))
        self._setting.add(setting)
        setting.add_done_callback(self._setting.discard)

    def received(self, event: dict[str, Any], parent: Route = ()) -> None:
        """Follow a message from a target attached through the session, which may tell of a target attached to it.

        Args:
            event: The `Target.receivedMessageFromTarget` event.
            parent: The sessions leading to the target it was attached through;
                none for the page.
        """
        route = (*parent, str(event["sessionId"]))
        message = cast("dict[str, Any]", json.loads(event["message"]))
        method = message.get("method")
        if method == "Target.attachedToTarget":
            self.attached(message["params"], route)
        elif method == "Target.receivedMessageFromTarget":
            self.received(message["params"], route)

    async def _set(self, route: Route, *, frame: bool) -> None:
        """Set a target's overrides, and attach to the frames and service workers of a frame; then let it run.

        The target's answers are not awaited: it handles the messages in order, so it
        runs once its overrides are set, or once setting them failed, as when its
        process holds them already.
        """
        try:
            for method, params in self._overrides:
                await self._send(route, method, params)
            if frame:
                await self._send(route, "Target.setAutoAttach", _RELATED)
        except Exception:  # the target, or the page, went away
            logger.debug("Setting the locale and the time zone of a target of a page failed", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await self._send(route, "Runtime.runIfWaitingForDebugger", {})

    async def _send(self, route: Route, method: str, params: dict[str, Any]) -> None:
        """Send a command to a target, through the session of each target that leads to it."""
        message = {"id": next(self._ids), "method": method, "params": params}
        for session in reversed(route[1:]):
            message = {
                "id": next(self._ids),
                "method": "Target.sendMessageToTarget",
                "params": {"sessionId": session, "message": json.dumps(message)},
            }
        await _send(
            self._devtools, "Target.sendMessageToTarget", {"sessionId": route[0], "message": json.dumps(message)}
        )


async def _send(devtools: CDPSession, method: str, params: dict[str, Any]) -> None:
    """Send a command of the DevTools protocol."""
    await devtools.send(method, params)  # pyright: ignore[reportUnknownMemberType]  # untyped
