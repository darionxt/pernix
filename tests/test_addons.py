from __future__ import annotations

import asyncio
from importlib.metadata import EntryPoint
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Response, Spider, events
from pernix.addons import Addon, AddonAPI, addon_settings, discover_addons, load_addons, setup_addon
from pernix.crawler import Crawler
from pernix.errors import AddonError
from pernix.events import EventBus
from pernix.extensions import Extension
from pernix.settings import CORE_SECTIONS, Section, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from loopback import LoopbackServer


class GreetingSettings(Section):
    __prefix__ = "greeting"
    text: str = "hello"


class Greeter(Extension):
    greetings: ClassVar[list[str]] = []

    def __init__(self, settings: GreetingSettings) -> None:
        self.text = settings.text

    @events.on(events.SpiderOpened)
    def opened(self, event: events.SpiderOpened) -> None:
        self.greetings.append(self.text)


class GreetingAddon(Addon):
    api_version = ">=1,<2"

    def update_settings(self, settings: dict[str, Any]) -> None:
        settings["concurrency"] = 3
        settings["extensions"] = {Greeter: 100}


class SubscribingAddon(Addon):
    api_version = ">=1"
    seen: ClassVar[list[str]] = []

    def setup(self, api: AddonAPI) -> None:
        api.on(events.SpiderClosed, lambda event: self.seen.append(f"{event.reason} at {api.settings['concurrency']}"))


class LateAddon(Addon):
    api_version = ">=1"
    api: AddonAPI | None = None

    def setup(self, api: AddonAPI) -> None:
        LateAddon.api = api


class AsyncSetupAddon(Addon):
    api_version = ">=1"

    async def setup(self, api: AddonAPI) -> None:  # type: ignore[override]
        pass


class FutureAddon(Addon):
    api_version = ">=2"


class UndeclaredAddon:
    pass


class MalformedAddon(Addon):
    api_version = "one or two"


@pytest.fixture(autouse=True)
def _reset() -> None:
    Greeter.greetings.clear()
    SubscribingAddon.seen.clear()


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Pretend packages advertise Addons through entry points."""
    advertised = {
        "greeting": f"{__name__}:GreetingAddon",
        "subscribing": f"{__name__}:SubscribingAddon",
        "broken": "no_such_module_xyz:Addon",
    }

    def fake_entry_points(*, group: str) -> list[EntryPoint]:
        assert group == "pernix.addons"
        return [EntryPoint(name=name, value=value, group=group) for name, value in advertised.items()]

    monkeypatch.setattr("pernix.addons.entry_points", fake_entry_points)
    return advertised


def _spider(server: LoopbackServer, **spider_settings: Any) -> type[Spider]:
    server.route("/", "ok")

    class Single(Spider):
        start_urls = (server.url("/"),)
        settings = spider_settings

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"ok": True}

    return Single


# Loading


def test_addons_load_by_entry_point_name_import_path_class_and_object(installed: dict[str, str]) -> None:
    obj = SubscribingAddon()

    loaded = load_addons(["greeting", f"{__name__}.SubscribingAddon", GreetingAddon, obj])

    assert [type(addon) for addon in loaded] == [GreetingAddon, SubscribingAddon, GreetingAddon, SubscribingAddon]
    assert loaded[3] is obj


def test_discovery_lists_installed_addons_without_loading_them(installed: dict[str, str]) -> None:
    assert sorted(discover_addons()) == ["broken", "greeting", "subscribing"]


def test_unknown_names_list_the_installed_addons(installed: dict[str, str]) -> None:
    with pytest.raises(AddonError, match=r"'missing' is neither an installed Addon \(installed: broken, greeting"):
        load_addons(["missing"])


def test_installed_addons_that_fail_to_import_are_reported(installed: dict[str, str]) -> None:
    with pytest.raises(AddonError, match="cannot load the installed Addon 'broken'"):
        load_addons(["broken"])


@pytest.mark.parametrize(
    ("addon", "message"),
    [
        (FutureAddon, r"requires Addon API >=2, but this Pernix provides 1\.0"),
        (UndeclaredAddon, "does not declare an api_version"),
        (MalformedAddon, "declares an invalid api_version 'one or two'"),
    ],
)
def test_incompatible_addons_are_rejected(addon: type, message: str) -> None:
    with pytest.raises(AddonError, match=message):
        load_addons([addon])


# Settings


def test_addon_settings_sit_below_the_project_file(tmp_path: Path) -> None:
    layer = addon_settings(GreetingAddon())
    with_project, without_project = tmp_path / "with", tmp_path / "without"
    with_project.mkdir()
    without_project.mkdir()
    (with_project / "pernix.toml").write_text("concurrency = 5\n")
    sections = (*CORE_SECTIONS, GreetingSettings)

    below_project = load_settings(sections=sections, addon_layers=[layer], project_dir=with_project, environ={})
    alone = load_settings(sections=sections, addon_layers=[layer], project_dir=without_project, environ={})

    assert below_project["concurrency"] == 5
    assert alone["concurrency"] == 3
    assert Greeter in alone.components("extensions")


async def test_enabled_addons_shape_the_run(
    server: LoopbackServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: dict[str, str]
) -> None:
    # The project file sets a key that only the Addon's Extension declares.
    (tmp_path / "pernix.toml").write_text('addons = ["greeting", "subscribing"]\n[greeting]\ntext = "hi"\n')
    monkeypatch.chdir(tmp_path)

    async with asyncio.timeout(10):
        await _spider(server).crawl()

    assert Greeter.greetings == ["hi"]
    assert SubscribingAddon.seen == ["finished at 3"]


async def test_installed_addons_are_not_loaded_unless_enabled(
    server: LoopbackServer, installed: dict[str, str]
) -> None:
    crawler = Crawler.from_spider(_spider(server))

    async with asyncio.timeout(10):
        await crawler.crawl()

    assert crawler.settings["addons"] == ()
    assert crawler.settings["concurrency"] == 16
    assert Greeter.greetings == []


# Setup


def test_setup_can_only_register_during_the_call(tmp_path: Path) -> None:
    bus = EventBus()
    settings = load_settings(environ={}, project_dir=tmp_path)

    setup_addon(LateAddon(), events=bus, settings=settings)

    assert LateAddon.api is not None
    with pytest.raises(AddonError, match="registered a subscriber after setup"):
        LateAddon.api.on(events.SpiderOpened, print)


def test_setup_must_be_synchronous(tmp_path: Path) -> None:
    settings = load_settings(environ={}, project_dir=tmp_path)

    with pytest.raises(AddonError, match=r"setup\(\) must be synchronous"):
        setup_addon(AsyncSetupAddon(), events=EventBus(), settings=settings)
