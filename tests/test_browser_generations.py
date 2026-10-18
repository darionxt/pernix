"""Generations of browsers, with stand-ins for patchright's: launched, replaced, lost, and closed."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from pernix.errors import BrowserLost, ComponentError
from pernix.fetchers import BrowserSettings, browser_generations
from pernix.fetchers.browser_generations import Generations
from pernix.stats import Stats

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.fetchers.launched_browser import LaunchedBrowser


class FakeBrowser:
    """Stands in for patchright's Browser: it can disconnect."""

    def __init__(self) -> None:
        self.connected = True
        self._listeners: dict[str, Callable[[FakeBrowser], None]] = {}

    def on(self, event: str, listener: Callable[[FakeBrowser], None]) -> None:
        self._listeners[event] = listener

    def is_connected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False
        self._listeners["disconnected"](self)


class FakeProcesses:
    """Stands in for the processes of a browser."""

    def __init__(self, memory: int = 0) -> None:
        self.bytes = memory
        self.killed = False
        self.driver_killed = False

    def memory(self) -> int:
        return self.bytes

    def remember(self) -> None:
        pass

    def driver_alive(self) -> bool:
        return not self.driver_killed

    def kill(self) -> None:
        self.killed = True

    def kill_driver(self) -> None:
        self.driver_killed = True


class FakeLaunched:
    """Stands in for a launched browser and its driver."""

    def __init__(self, number: int, memory: int) -> None:
        self.number = number
        self.browser = FakeBrowser()
        self.processes = FakeProcesses(memory)
        self.closed = False
        self.answers = True

    async def probe(self) -> None:
        if not self.answers:
            await asyncio.sleep(3600)

    async def close(self) -> None:
        self.closed = True


class Launcher:
    """Launches FakeLaunched browsers, numbered from 1, recording them."""

    def __init__(self, *, memory: int = 0, failures: list[Exception] | None = None) -> None:
        self.launched: list[FakeLaunched] = []
        self.memory = memory
        self.failures = failures or []

    async def __call__(self, _settings: BrowserSettings) -> LaunchedBrowser:
        await asyncio.sleep(0.01)
        if self.failures:
            raise self.failures.pop(0)
        launched = FakeLaunched(len(self.launched) + 1, self.memory)
        self.launched.append(launched)
        return cast("LaunchedBrowser", launched)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _generations(
    launcher: Launcher, *, clock: Clock | None = None, **settings: Any
) -> tuple[Generations, list[FakeLaunched], Stats]:
    retired: list[FakeLaunched] = []
    stats = Stats()
    generations = Generations(
        BrowserSettings(**settings),
        retire=lambda launched: retired.append(cast("FakeLaunched", launched)),
        stats=stats,
        launch=launcher,
        clock=clock or Clock(),
    )
    return generations, retired, stats


async def _number(generations: Generations) -> int:
    """Fetch once, and tell which browser the fetch used."""
    async with generations.fetching("https://shop.example/") as generation:
        return cast("FakeLaunched", generation.launched).number


async def test_the_fetches_that_wait_for_a_browser_share_one_launch() -> None:
    launcher = Launcher()
    generations, _, stats = _generations(launcher)

    numbers = await asyncio.gather(*(_number(generations) for _ in range(4)))

    assert numbers == [1, 1, 1, 1]
    assert stats.get("browser.generations") == 1
    await generations.close()
    assert launcher.launched[0].closed


async def test_the_fetches_that_waited_for_a_launch_keep_to_the_page_limit() -> None:
    launcher = Launcher()
    generations, _, stats = _generations(launcher, generation_pages=1)

    numbers = await asyncio.gather(*(_number(generations) for _ in range(3)))

    assert sorted(numbers) == [1, 2, 3]  # one page each, though all three waited for the first launch
    assert stats.get("browser.replaced_by_reason.pages") == 2
    await generations.close()


async def test_a_browser_is_replaced_after_its_pages_and_closed_once_its_fetches_end() -> None:
    launcher = Launcher()
    generations, retired, stats = _generations(launcher, generation_pages=2)
    inside, release = asyncio.Event(), asyncio.Event()

    async def held() -> int:
        async with generations.fetching("https://shop.example/") as generation:
            inside.set()
            await release.wait()
            return cast("FakeLaunched", generation.launched).number

    first = asyncio.create_task(held())
    await inside.wait()
    assert await _number(generations) == 1  # its second page
    assert await _number(generations) == 2  # the first browser opened its pages
    old = launcher.launched[0]
    assert (old.closed, retired) == (False, [old])  # a fetch still runs there
    release.set()

    assert await first == 1
    async with asyncio.timeout(5):
        while not old.processes.killed:  # noqa: ASYNC110  # closed in a task of its own, then killed
            await asyncio.sleep(0.01)
    assert old.closed
    assert (stats.get("browser.replaced"), stats.get("browser.replaced_by_reason.pages")) == (1, 1)
    await generations.close()


async def test_a_browser_is_replaced_once_it_is_old() -> None:
    clock = Clock()
    launcher = Launcher()
    generations, _, stats = _generations(launcher, generation_age=60.0, clock=clock)

    assert await _number(generations) == 1
    clock.now = 59.0
    assert await _number(generations) == 1
    clock.now = 60.0
    assert await _number(generations) == 2
    assert stats.get("browser.replaced_by_reason.age") == 1
    await generations.close()


async def test_0_never_replaces_a_browser() -> None:
    clock = Clock()
    launcher = Launcher()
    generations, _, _ = _generations(launcher, generation_age=0, generation_pages=0, clock=clock)

    clock.now = 1e9
    numbers = [await _number(generations) for _ in range(5)]

    assert numbers == [1] * 5
    await generations.close()


async def test_a_browser_that_disconnects_fails_its_fetches_and_the_next_launches_another() -> None:
    launcher = Launcher()
    generations, retired, stats = _generations(launcher)
    inside = asyncio.Event()

    async def held() -> None:
        async with generations.fetching("https://shop.example/"):
            inside.set()
            await asyncio.sleep(30)

    fetching = asyncio.create_task(held())
    await inside.wait()
    launcher.launched[0].browser.disconnect()

    with pytest.raises(BrowserLost, match=r"rendering https://shop.example/ failed: the browser disconnected"):
        await fetching
    assert await _number(generations) == 2
    old = launcher.launched[0]
    assert (old.closed, old.processes.killed, retired) == (True, True, [old])
    assert (stats.get("browser.lost"), stats.get("browser.lost_by_reason.disconnected")) == (1, 1)
    await generations.close()


async def test_a_fetch_failing_as_the_driver_died_loses_the_browser() -> None:
    launcher = Launcher()
    generations, _, stats = _generations(launcher)

    with pytest.raises(BrowserLost, match="patchright's driver died") as lost:
        async with generations.fetching("https://shop.example/"):
            raise Exception("Page.goto: Connection closed while reading from the driver")  # as patchright raises it

    assert "Connection closed" in str(lost.value.__cause__)
    assert stats.get("browser.lost_by_reason.driver") == 1
    assert await _number(generations) == 2
    await generations.close()


async def test_other_errors_leave_the_browser_as_it_is() -> None:
    launcher = Launcher()
    generations, _, stats = _generations(launcher)

    with pytest.raises(ValueError, match="bad selector"):
        async with generations.fetching("https://shop.example/"):
            raise ValueError("bad selector")

    assert stats.get("browser.lost") is None
    assert await _number(generations) == 1
    await generations.close()


async def test_a_browser_that_stops_answering_its_probe_is_lost_and_its_driver_killed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(browser_generations, "PROBE_INTERVAL", 0.01)
    monkeypatch.setattr(browser_generations, "PROBE_TIMEOUT", 0.05)
    launcher = Launcher()
    generations, _, stats = _generations(launcher)
    inside = asyncio.Event()

    async def held() -> None:
        async with generations.fetching("https://shop.example/"):
            launcher.launched[0].answers = False
            inside.set()
            await asyncio.sleep(30)

    fetching = asyncio.create_task(held())
    await inside.wait()

    with pytest.raises(BrowserLost, match="the browser stopped answering"):
        await fetching
    assert launcher.launched[0].processes.driver_killed
    assert stats.get("browser.lost_by_reason.unresponsive") == 1
    await generations.close()


async def test_a_browser_over_its_memory_is_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_generations, "PROBE_INTERVAL", 0.01)
    launcher = Launcher(memory=3 * 1024 * 1024)
    generations, _, stats = _generations(launcher, generation_memory=2)

    assert await _number(generations) == 1
    async with asyncio.timeout(5):
        while stats.get("browser.replaced_by_reason.memory") is None:  # noqa: ASYNC110  # the probe replaces it
            await asyncio.sleep(0.01)

    assert await _number(generations) == 2
    assert launcher.launched[0].closed  # no fetch was left in it
    await generations.close()


async def test_a_launch_that_failed_is_tried_again_by_the_next_fetch() -> None:
    launcher = Launcher(failures=[RuntimeError("the browser does not start")])
    generations, _, _ = _generations(launcher)

    with pytest.raises(RuntimeError, match="does not start"):
        await _number(generations)

    assert await _number(generations) == 1
    await generations.close()


async def test_a_missing_engine_fails_every_fetch_until_opened_again() -> None:
    launcher = Launcher(failures=[ComponentError("the browser engine is not installed")])
    generations, _, _ = _generations(launcher)

    for _ in range(2):
        with pytest.raises(ComponentError, match="not installed"):
            await _number(generations)
    generations.open()

    assert await _number(generations) == 1
    await generations.close()


async def test_closing_closes_every_browser_and_kills_their_processes() -> None:
    launcher = Launcher()
    generations, _, _ = _generations(launcher, generation_pages=1)
    inside, release = asyncio.Event(), asyncio.Event()

    async def held() -> None:
        async with generations.fetching("https://shop.example/"):
            inside.set()
            await release.wait()

    first = asyncio.create_task(held())
    await inside.wait()
    assert await _number(generations) == 2  # the first one is replaced, and still in use

    release.set()
    await first
    await generations.close()

    assert [(launched.closed, launched.processes.killed) for launched in launcher.launched] == [(True, True)] * 2
