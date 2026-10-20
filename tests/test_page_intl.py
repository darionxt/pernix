"""Presenting a locale and a time zone in a page's targets, with a stand-in DevTools session."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, cast

import pytest

from pernix.fetchers.page_intl import present_intl

if TYPE_CHECKING:
    from collections.abc import Callable

    from patchright.async_api import BrowserContext, Page

LOCALE = ("Emulation.setLocaleOverride", {"locale": "de-DE"})
TIMEZONE = ("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Berlin"})
RELATED = {
    "autoAttach": True,
    "waitForDebuggerOnStart": True,
    "flatten": False,
    "filter": [{"type": "iframe"}, {"type": "service_worker"}],
}


class DevTools:
    """A stand-in for a page's DevTools session: it records the commands sent, and fails those it is told to."""

    def __init__(
        self, failing: Callable[[str, dict[str, Any]], Exception | None] = lambda _method, _params: None
    ) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.arriving: asyncio.Queue[tuple[tuple[str, ...], str]] = asyncio.Queue()
        self._failing = failing

    def on(self, event: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self.handlers[event] = handler

    async def send(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))
        route, command = _unwrapped(method, params)
        self.arriving.put_nowait((route, command["method"]))
        failure = self._failing(method, params)
        if failure is not None:
            raise failure

    def attach(self, session: str, kind: str, *, through: tuple[str, ...] = ()) -> None:
        """Tell of a target attached, through the targets of `through`, outermost first."""
        event = {"sessionId": session, "targetInfo": {"type": kind}, "waitingForDebugger": True}
        self._tell("Target.attachedToTarget", event, through)

    def detach(self, session: str, *, through: tuple[str, ...] = ()) -> None:
        """Tell of a target detached, through the targets of `through`, outermost first."""
        self._tell("Target.detachedFromTarget", {"sessionId": session}, through)

    def _tell(self, method: str, event: dict[str, Any], through: tuple[str, ...]) -> None:
        for parent in reversed(through):
            message = json.dumps({"method": method, "params": event})
            event, method = {"sessionId": parent, "message": message}, "Target.receivedMessageFromTarget"
        self.handlers[method](event)

    def commands(self) -> list[tuple[tuple[str, ...], str, dict[str, Any]]]:
        """The commands sent, each with the sessions it went through; a target's own session is the last."""
        unwrapped = [_unwrapped(method, params) for method, params in self.sent]
        return [(route, command["method"], command["params"]) for route, command in unwrapped]


class Context:
    def __init__(self, devtools: DevTools) -> None:
        self.devtools = devtools

    async def new_cdp_session(self, _page: object) -> DevTools:
        return self.devtools


def _unwrapped(method: str, params: dict[str, Any]) -> tuple[tuple[str, ...], dict[str, Any]]:
    route: list[str] = []
    command: dict[str, Any] = {"method": method, "params": params}
    while command["method"] == "Target.sendMessageToTarget":
        route.append(command["params"]["sessionId"])
        command = json.loads(command["params"]["message"])
    return tuple(route), command


async def _present(devtools: DevTools, *, timezone: str | None = "Europe/Berlin") -> None:
    context = cast("BrowserContext", Context(devtools))
    await present_intl(context, cast("Page", object()), locale="de-DE", timezone=timezone)


async def _arrived(devtools: DevTools, method: str, count: int) -> list[tuple[str, ...]]:
    """The targets the next `count` commands of a method went to, once they were sent."""
    routes: list[tuple[str, ...]] = []
    async with asyncio.timeout(5):  # a target never let run would hang
        while len(routes) < count:
            route, sent = await devtools.arriving.get()
            if sent == method:
                routes.append(route)
    return routes


async def _ran(devtools: DevTools, count: int) -> list[tuple[str, ...]]:
    return await _arrived(devtools, "Runtime.runIfWaitingForDebugger", count)


async def test_the_page_takes_the_locale_and_its_cross_site_frames_and_service_workers_are_attached() -> None:
    devtools = DevTools()

    await _present(devtools)

    assert devtools.commands() == [((), *LOCALE), ((), "Target.setAutoAttach", RELATED)]


async def test_a_page_whose_process_holds_the_locale_already_goes_on() -> None:
    def failing(method: str, _params: dict[str, Any]) -> Exception | None:
        if method == "Emulation.setLocaleOverride":
            return RuntimeError("Protocol error: Another locale override is already in effect")
        return None

    devtools = DevTools(failing)

    await _present(devtools)

    assert devtools.commands()[-1] == ((), "Target.setAutoAttach", RELATED)


async def test_any_other_failure_to_set_the_locale_of_the_page_is_raised() -> None:
    devtools = DevTools(lambda method, _params: RuntimeError("Target closed") if "Locale" in method else None)

    with pytest.raises(RuntimeError, match="Target closed"):
        await _present(devtools)


async def test_a_cross_site_frame_takes_both_and_attaches_its_own_before_it_runs() -> None:
    devtools = DevTools()
    await _present(devtools)

    devtools.attach("frame", "iframe")

    assert await _ran(devtools, 1) == [("frame",)]
    assert devtools.commands()[2:] == [
        (("frame",), *LOCALE),
        (("frame",), *TIMEZONE),
        (("frame",), "Target.setAutoAttach", RELATED),
        (("frame",), "Runtime.runIfWaitingForDebugger", {}),
    ]


async def test_a_target_within_a_cross_site_frame_is_reached_through_it() -> None:
    devtools = DevTools()
    await _present(devtools, timezone=None)

    devtools.attach("worker", "service_worker", through=("outer", "inner"))

    assert await _ran(devtools, 1) == [("outer", "inner", "worker")]
    assert devtools.commands()[2:] == [
        (("outer", "inner", "worker"), *LOCALE),
        (("outer", "inner", "worker"), "Runtime.runIfWaitingForDebugger", {}),
    ]


async def test_a_target_runs_even_when_its_overrides_cannot_be_sent() -> None:
    def failing(method: str, params: dict[str, Any]) -> Exception | None:
        if method == "Target.sendMessageToTarget" and "Locale" in params["message"]:
            return RuntimeError("Target closed")
        return None

    devtools = DevTools(failing)
    await _present(devtools, timezone=None)

    devtools.attach("frame", "iframe")

    assert await _ran(devtools, 1) == [("frame",)]
    assert [command for _route, command, _params in devtools.commands()[2:]] == [
        "Emulation.setLocaleOverride",
        "Runtime.runIfWaitingForDebugger",
    ]


async def test_once_a_target_is_detached_the_others_take_both_again() -> None:
    devtools = DevTools()
    await _present(devtools)
    for session in ("gone", "left", "other"):
        devtools.attach(session, "iframe")
    devtools.attach("within", "iframe", through=("gone",))
    await _ran(devtools, 4)
    sent = len(devtools.sent)

    devtools.detach("gone")

    assert await _arrived(devtools, TIMEZONE[0], 2) == [("left",), ("other",)]
    assert devtools.commands()[sent:] == [
        (("left",), *LOCALE),
        (("left",), *TIMEZONE),
        (("other",), *LOCALE),
        (("other",), *TIMEZONE),
    ]


async def test_a_target_detached_within_another_is_followed() -> None:
    devtools = DevTools()
    await _present(devtools, timezone=None)
    devtools.attach("frame", "iframe")
    devtools.attach("within", "iframe", through=("frame",))
    await _ran(devtools, 2)
    sent = len(devtools.sent)

    devtools.detach("within", through=("frame",))

    assert await _arrived(devtools, LOCALE[0], 1) == [("frame",)]
    assert devtools.commands()[sent:] == [(("frame",), *LOCALE)]
