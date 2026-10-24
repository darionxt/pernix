"""Presenting a locale and a time zone in a page's `Intl`, and in its frames and web workers, without moving a header."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Coroutine

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

A service worker runs all the same once patchright's own session, which attaches
to it too, tells it to. Not flattened, as a patchright `CDPSession` can reach the
session of a target attached to it only through `Target.sendMessageToTarget`.
Dedicated workers run in the process of the frame that starts them, so they are
left alone.
"""

_ALREADY_OVERRIDDEN = "Another locale override is already in effect"
"""What Chrome answers a target whose process already holds an override, which its targets share."""


async def present_intl(context: BrowserContext, page: Page, *, locale: str | None, timezone: str | None) -> None:
    """Make a page, before it loads anything, present a locale and a time zone in `Intl`, with its frames and workers.

    Chrome's `Emulation.setLocaleOverride` and `Emulation.setTimezoneOverride` hold
    for the renderer process of the target they are sent to, so for the page's
    same-site frames and the dedicated and shared workers that run in it. A
    DevTools session of the page's own sets the locale, and attaches to the page's
    cross-site frames, at any depth, pausing each until both are set, and to its
    service workers, which may run in processes of their own; it lets each run
    whatever failed. A service worker runs as soon as patchright's own session,
    attached to it too, lets it: one the page starts runs in the process of the
    page, or of a frame of its site, which presents both already, and one in a
    process of its own takes them a moment after it starts. The service workers of
    the context already running, as one an earlier page of the context started, are
    attached and sent both before this returns, so before the page navigates and
    can reach them. patchright's `locale` option would set the locale too, but it
    moves `Accept-Language` after `User-Agent`. The page's time zone, and that of
    its frames, is patchright's `timezone_id`; a service worker in a process of its
    own needs its own.

    An override belongs to the target whose session set it first in a process: the
    others sharing the process are refused it, and it ends with that target, so
    once a frame or a service worker goes, both are set again in those left, which
    present the machine's locale, time zone, or both meanwhile, as another target
    may have held either. The page's own last as long as the session, which closes
    with the page. A page the page opens in another process, as with `noopener`,
    presents the machine's locale.

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
    devtools.on("Target.detachedFromTarget", related.detached)
    devtools.on("Target.receivedMessageFromTarget", related.received)
    if locale is not None:
        try:
            await _send(devtools, "Emulation.setLocaleOverride", {"locale": locale})
        except Exception as exc:  # the page's process holds the override already
            if _ALREADY_OVERRIDDEN not in str(exc):
                raise
    await _send(devtools, "Target.setAutoAttach", _RELATED)
    if context.service_workers:  # the auto-attach would reach them only once the page navigates into their scope
        await _attach_running_service_workers(devtools)
        await related.settled()


async def _attach_running_service_workers(devtools: CDPSession) -> None:
    """Attach a page's session to its context's running service workers, set up then as any target it attaches.

    Chrome tells of each with `Target.attachedToTarget` before it answers, so each
    is being set up once this returns.
    """
    page = await _send(devtools, "Target.getTargetInfo", {})
    context_id = page["targetInfo"]["browserContextId"]
    targets = await _send(devtools, "Target.getTargets", {})
    for target in cast("list[dict[str, Any]]", targets["targetInfos"]):
        if target["type"] != "service_worker" or target.get("browserContextId") != context_id:
            continue
        try:
            await _send(devtools, "Target.attachToTarget", {"targetId": target["targetId"], "flatten": False})
        except Exception:  # the worker stopped meanwhile
            logger.debug("Attaching to a running service worker of a page's context failed", exc_info=True)


class _RelatedTargets:
    """Sets the locale and the time zone of each target attached through a page's DevTools session, then lets it run.

    Once one is detached, they are set again in the others, as it may have held
    them for a process it shared with some.
    """

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
        self._targets: dict[Route, None] = {}
        """The targets attached, by the sessions leading to them, in the order they were."""
        self._setting: set[asyncio.Task[None]] = set()

    def attached(self, event: dict[str, Any], parent: Route = ()) -> None:
        """Set the overrides of a target just attached, in a task of its own.

        Args:
            event: The `Target.attachedToTarget` event.
            parent: The sessions leading to the target it was attached through;
                none for the page.
        """
        route = (*parent, str(event["sessionId"]))
        self._targets[route] = None
        self._start(self._set(route, frame=event["targetInfo"]["type"] == "iframe"))

    def detached(self, event: dict[str, Any], parent: Route = ()) -> None:
        """Forget a target detached, and those attached through it, and set the overrides again in the others.

        Args:
            event: The `Target.detachedFromTarget` event.
            parent: The sessions leading to the target it was attached through;
                none for the page.
        """
        route = (*parent, str(event["sessionId"]))
        gone = [target for target in self._targets if target[: len(route)] == route]
        for target in gone:
            del self._targets[target]
        if gone and self._targets and self._overrides:
            self._start(self._set_again(list(self._targets)))

    async def settled(self) -> None:
        """Wait until the targets attached so far were sent their overrides, and told to run."""
        if self._setting:
            await asyncio.wait(tuple(self._setting))  # left running, were the wait cancelled

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
        elif method == "Target.detachedFromTarget":
            self.detached(message["params"], route)
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

    async def _set_again(self, routes: list[Route]) -> None:
        """Set the overrides of targets running already: those whose process lost them take them; the others refuse."""
        for route in routes:
            for method, params in self._overrides:
                try:
                    await self._send(route, method, params)
                except Exception:  # the target, or the page, went away
                    logger.debug(
                        "Setting the locale and the time zone of a target of a page again failed", exc_info=True
                    )

    def _start(self, setting: Coroutine[Any, Any, None]) -> None:
        """Run a coroutine sending overrides in a task of its own, kept until it ends."""
        task = asyncio.get_running_loop().create_task(setting)
        self._setting.add(task)
        task.add_done_callback(self._setting.discard)

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


async def _send(devtools: CDPSession, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send a command of the DevTools protocol, and return its result."""
    return cast("dict[str, Any]", await devtools.send(method, params))  # pyright: ignore[reportUnknownMemberType]  # untyped
