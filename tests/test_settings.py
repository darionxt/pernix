from __future__ import annotations

from typing import TYPE_CHECKING

import msgspec
import pytest

from pernix.settings import ComponentMap, CoreSettings, Section, load_settings

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.settings import Settings


class PluginSettings(Section):
    __prefix__ = "test"
    plugins: ComponentMap = msgspec.field(default_factory=dict)


class Unregistered(Section):
    __prefix__ = "unregistered"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings(
        sections=(CoreSettings, PluginSettings),
        project_dir=tmp_path,
        environ={},
        overrides={"concurrency": 4, "test.plugins": {"collections.OrderedDict": 1}},
    )


HTTP_KEYS = (
    "impersonate",
    "timeout",
    "deadline",
    "max_redirects",
    "max_body_size",
    "pool_size",
    "pool_idle",
    "inherit_referer",
)
BROWSER_KEYS = (
    "headless",
    "timeout",
    "deadline",
    "launch_options",
    "max_pages",
    "generation_age",
    "generation_pages",
    "generation_memory",
    "max_idle_contexts",
    "context_idle",
    "block",
    "action_timeout",
)


def test_settings_are_a_read_only_mapping(settings: Settings) -> None:
    assert settings["concurrency"] == 4
    assert "concurrency" in settings
    assert set(settings) == {
        "concurrency",
        "extensions",
        "addons",
        "middlewares",
        "spider_middlewares",
        "fetchers",
        "pipelines",
        "failed_pipelines",
        "run_id",
        "locale",
        "timezone",
        "test.plugins",
        *(f"http.{name}" for name in HTTP_KEYS),  # registered by the default HTTP Fetcher
        *(f"browser.{name}" for name in BROWSER_KEYS),  # and by the default browser Fetcher
    }

    with pytest.raises(TypeError):
        settings["concurrency"] = 8  # type: ignore[index]
    with pytest.raises(TypeError):
        settings["test.plugins"][dict] = 1
    with pytest.raises(AttributeError):
        settings.section(CoreSettings).concurrency = 8  # type: ignore[misc]


def test_unknown_keys_raise_with_a_suggestion(settings: Settings) -> None:
    with pytest.raises(KeyError, match=r"unknown setting 'concurency'; did you mean 'concurrency'\?"):
        settings["concurency"]


def test_unregistered_sections_raise(settings: Settings) -> None:
    with pytest.raises(KeyError, match="Unregistered is not a registered settings section"):
        settings.section(Unregistered)


def test_components_resolves_import_paths(settings: Settings) -> None:
    from collections import OrderedDict

    assert settings.components("test.plugins") == [OrderedDict]


def test_components_rejects_settings_that_are_not_component_maps(settings: Settings) -> None:
    with pytest.raises(TypeError, match="'concurrency' is not a component map"):
        settings.components("concurrency")


def test_origin_describes_the_winning_layer(settings: Settings) -> None:
    assert settings.origin("concurrency") == "run() arguments"
    with pytest.raises(KeyError):
        settings.origin("concurency")
