from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Pipeline, Spider
from pernix.crawler import Crawler
from pernix.errors import ComponentError, SettingsError
from pernix.extensions import OffsiteFilter
from pernix.fetchers import BrowserFetcher, HTTPFetcher
from pernix.items import FailedItemsFile
from pernix.logs import RunContext, RunLog
from pernix.proxies import Binding, DefaultBindingPolicy
from pernix.proxies.proxy_sources import ProxyAPI
from pernix.proxies.run_proxies import RunProxies
from pernix.run_assembly import RunAssembly
from pernix.settings import LogSettings

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


class Plain(Spider):
    pass


class Recorded:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name, self.calls = name, calls

    async def open(self) -> None:
        self.calls.append(f"open {self.name}")


class Store(Pipeline):
    pass


def _build(settings: Mapping[str, Any], **options: Any) -> RunAssembly:
    crawler = Crawler.from_spider(Plain, settings=settings)
    run_log = RunLog(LogSettings(), RunContext(crawler.run_id, crawler.spider.name))
    return RunAssembly.build(crawler, run_log, **options)


def test_the_components_are_built_in_opening_order_and_none_is_opened() -> None:
    calls: list[str] = []
    late, early = Recorded("late", calls), Recorded("early", calls)

    run = _build({"extensions": {late: 300, early: 200}, "pipelines": {Store: 100}})

    assert [type(component) for component in run.components] == [
        OffsiteFilter,
        Recorded,
        Recorded,
        HTTPFetcher,
        BrowserFetcher,
        Store,
        FailedItemsFile,
    ]
    assert run.components[1:3] == (early, late)
    assert calls == []


def test_the_sources_of_proxies_and_the_proxies_are_opened_before_the_fetchers() -> None:
    class Provider:
        async def proxies(self) -> list[str]:
            return []

    provider = Provider()

    run = _build({"proxies.api": "http://vendor.example/proxies", "proxies.provider": provider})

    assert [type(component) for component in run.components] == [
        OffsiteFilter,
        ProxyAPI,
        Provider,
        DefaultBindingPolicy,
        RunProxies,
        HTTPFetcher,
        BrowserFetcher,
        FailedItemsFile,
    ]
    assert run.components[2] is provider


def test_a_binding_policy_of_ones_own_is_built_and_opened_before_the_proxies() -> None:
    class ByTag:
        async def bind(self, binding: Binding) -> str | None:
            return None

    policy = ByTag()

    run = _build({"proxies.urls": ["a.example:1"], "proxies.binding": policy})

    assert [type(component) for component in run.components[1:3]] == [ByTag, RunProxies]
    assert run.components[1] is policy


class Sticky:
    async def bind(self, binding: Binding) -> str | None:
        return None


def test_a_binding_policy_without_a_source_gives_the_sessions_of_a_pool_their_proxies() -> None:
    run = _build({"sessions.enabled": True, "proxies.binding": Sticky})

    assert [type(component) for component in run.components[1:3]] == [Sticky, RunProxies]


def test_a_binding_policy_without_a_source_or_a_session_pool_fails_the_build() -> None:
    with pytest.raises(SettingsError, match=r"proxies\.binding: without a source of proxies") as refused:
        _build({"proxies.binding": Sticky})

    assert "enable one with sessions.enabled" in str(refused.value)


def test_obeying_robots_txt_without_an_http_fetcher_fails_the_build() -> None:
    with pytest.raises(
        SettingsError, match=r"robots\.obey: robots\.txt is fetched with the Fetcher registered as 'http'"
    ):
        _build({"robots.obey": True, "fetchers": {"http": None}})


def test_a_product_token_is_letters_underscores_and_hyphens_or_a_star() -> None:
    _build({"robots.user_agent": "Example_Bot-News"})
    _build({"robots.user_agent": "*"})
    with pytest.raises(SettingsError, match=r"robots\.user_agent: 'ExampleBot/1\.0' is not a product token"):
        _build({"robots.user_agent": "ExampleBot/1.0"})


def test_never_rebinding_while_refreshes_replace_the_proxies_is_warned_about(caplog: pytest.LogCaptureFixture) -> None:
    _build({"proxies.urls": ["a.example:1"], "proxies.rebind": "never", "proxies.refresh": 60})

    assert "proxies.rebind is never and proxies.refresh is set" in caplog.text


def test_a_binding_policy_without_bind_fails_the_build() -> None:
    with pytest.raises(ComponentError, match=r"proxies\.binding must have bind\(binding\)"):
        _build({"proxies.urls": ["a.example:1"], "proxies.binding": object()})


def test_a_provider_without_proxies_fails_the_build() -> None:
    with pytest.raises(ComponentError, match=r"proxies\.provider must have proxies\(\)"):
        _build({"proxies.provider": object()})


def test_a_component_that_does_not_fit_its_role_fails_the_build() -> None:
    with pytest.raises(ComponentError, match=r"retry\.policy must have decide"):
        _build({"retry.policy": object()})


def test_two_outputs_may_not_write_the_same_file(tmp_path: Path) -> None:
    path = str(tmp_path / "books.jsonl")

    with pytest.raises(SettingsError, match=r"feeds\['.*books.jsonl'\] and output both write"):
        _build({"feeds": {path: {}}}, output=path)


def test_the_output_and_the_outlet_reach_the_item_processing(tmp_path: Path) -> None:
    async def outlet(_item: Any, _processed: Callable[[], None]) -> None:
        pass

    run = _build({}, output=tmp_path / "books.csv", outlet=outlet)

    assert [feed.path for feed in run.feeds] == [tmp_path / "books.csv"]
    assert run.items._outlet is outlet  # pyright: ignore[reportPrivateUsage]


def test_independent_problems_are_reported_together(tmp_path: Path) -> None:
    path = str(tmp_path / "books.jsonl")

    with pytest.raises(SettingsError) as raised:
        _build({"middlewares": {object(): 10}, "retry.policy": object(), "feeds": {path: {}}}, output=path)

    problems = raised.value.problems
    assert len(problems) == 3
    assert any("is not an async callable" in problem for problem in problems)
    assert any(problem.startswith("retry.policy must have decide") for problem in problems)
    assert any("and output both write" in problem for problem in problems)
    cause = raised.value.__cause__
    assert isinstance(cause, ExceptionGroup)
    assert [type(error) for error in cause.exceptions] == [ComponentError, SettingsError, ComponentError]
