from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING, Any, Protocol

import msgspec
import pytest

from pernix import Response, Spider
from pernix.crawler import Crawler
from pernix.errors import ComponentError
from pernix.settings import CORE_SECTIONS, ComponentMap, Section, Settings, load_settings
from pernix.testing import ReceivedRequest, Reply

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from pernix.testing import LoopbackServer


class PluginSettings(Section):
    __prefix__ = "test"
    plugins: ComponentMap = msgspec.field(default_factory=dict)


class GreetingSettings(Section):
    __prefix__ = "greeting"
    text: str = "hello"


class Greeter:
    def __init__(self, crawler: Crawler, settings: Settings, greeting: GreetingSettings) -> None:
        self.crawler = crawler
        self.settings = settings
        self.greeting = greeting


class NeedsUnregisteredSection:
    def __init__(self, other: OtherSettings) -> None:
        self.other = other


class OtherSettings(Section):
    __prefix__ = "other"


class Hooks(Protocol):
    def on_open(self) -> None: ...


class NoHooks:
    def on_open(self) -> None:
        pass


NO_HOOKS = NoHooks()


class Configurable:
    def __init__(self, crawler: Crawler, options: Any = None, hooks: Hooks = NO_HOOKS, fallback: object = None) -> None:
        self.crawler = crawler
        self.options = options
        self.hooks = hooks
        self.fallback = fallback


class NeedsAnything:
    def __init__(self, options: Any) -> None:
        self.options = options


class Idle(Spider):
    pass


def _crawler(tmp_path: Path, **overrides: Any) -> Crawler:
    settings = load_settings(
        sections=(*CORE_SECTIONS, PluginSettings), project_dir=tmp_path, environ={}, overrides=overrides
    )
    return Crawler(Idle(), settings=settings)


def test_build_component_injects_crawler_settings_and_sections(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path, **{"test.plugins": {Greeter: 1}, "greeting.text": "hi"})

    [spec] = crawler.settings.components("test.plugins")
    greeter = crawler.build_component(spec)

    assert isinstance(greeter, Greeter)
    assert greeter.crawler is crawler
    assert greeter.settings is crawler.settings
    assert greeter.greeting == GreetingSettings(text="hi")


def test_build_component_cannot_inject_unregistered_sections(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)

    with pytest.raises(ComponentError, match="required parameter 'other'"):
        crawler.build_component(NeedsUnregisteredSection)


def test_only_exact_service_types_are_injected(tmp_path: Path) -> None:
    crawler = _crawler(tmp_path)

    component = crawler.build_component(Configurable)

    assert isinstance(component, Configurable)
    assert component.crawler is crawler
    assert (component.options, component.hooks, component.fallback) == (None, NO_HOOKS, None)
    with pytest.raises(ComponentError, match="required parameter 'options'"):
        crawler.build_component(NeedsAnything)


def test_crawlers_do_not_share_settings(tmp_path: Path) -> None:
    first = _crawler(tmp_path, concurrency=1)
    second = _crawler(tmp_path, concurrency=2)

    assert (first.settings["concurrency"], second.settings["concurrency"]) == (1, 2)


def _tracking_handler() -> tuple[Any, list[int]]:
    """A handler that records how many requests the server is handling at once."""
    lock = threading.Lock()
    state = {"current": 0}
    peaks: list[int] = []

    def handle(_request: ReceivedRequest) -> Reply:
        with lock:
            state["current"] += 1
            peaks.append(state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return Reply(body="ok")

    return handle, peaks


@pytest.mark.parametrize(("source", "concurrency"), [("spider", 1), ("run", 1), ("spider", 3), ("run", 3)])
async def test_concurrency_setting_bounds_requests_in_flight(
    server: LoopbackServer, source: str, concurrency: int
) -> None:
    handle, peaks = _tracking_handler()
    for n in range(6):
        server.route(f"/{n}", handler=handle)

    class Parallel(Spider):
        start_urls = tuple(server.url(f"/{n}") for n in range(6))
        settings = {"concurrency": concurrency} if source == "spider" else {}

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"url": response.url}

    async with asyncio.timeout(10):
        await Parallel.crawl(settings={"concurrency": concurrency} if source == "run" else None)

    assert len(peaks) == 6
    assert max(peaks) == concurrency


SAME_NAMED_PLUGIN = """
from __future__ import annotations

from pernix.settings import Section


class PluginSettings(Section):
    __prefix__ = "{prefix}"
    value: str = "{prefix}-default"


class Plugin:
    def __init__(self, settings: PluginSettings) -> None:
        self.settings = settings
"""


def test_same_named_sections_in_different_modules_stay_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for prefix in ("alpha", "beta"):
        (tmp_path / f"pernix_plugin_{prefix}.py").write_text(SAME_NAMED_PLUGIN.format(prefix=prefix))
    monkeypatch.syspath_prepend(str(tmp_path))
    plugins = {"pernix_plugin_alpha.Plugin": 1, "pernix_plugin_beta.Plugin": 2}

    crawler = _crawler(tmp_path, **{"test.plugins": plugins, "beta.value": "set"})
    alpha, beta = (crawler.build_component(spec) for spec in crawler.settings.components("test.plugins"))

    assert alpha.settings.value == "alpha-default"  # type: ignore[attr-defined]
    assert beta.settings.value == "set"  # type: ignore[attr-defined]
