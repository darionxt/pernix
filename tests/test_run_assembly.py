from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Pipeline, Spider
from pernix.crawler import Crawler
from pernix.errors import ComponentError, SettingsError
from pernix.extensions import OffsiteFilter
from pernix.fetchers import HTTPFetcher
from pernix.items import FailedItemsFile
from pernix.logs import RunContext, RunLog
from pernix.run_assembly import RunAssembly
from pernix.settings import LogSettings

if TYPE_CHECKING:
    from collections.abc import Mapping
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
        Store,
        FailedItemsFile,
    ]
    assert run.components[1:3] == (early, late)
    assert calls == []


def test_a_component_that_does_not_fit_its_role_fails_the_build() -> None:
    with pytest.raises(ComponentError, match=r"retry\.policy must have decide"):
        _build({"retry.policy": object()})


def test_two_outputs_may_not_write_the_same_file(tmp_path: Path) -> None:
    path = str(tmp_path / "books.jsonl")

    with pytest.raises(SettingsError, match=r"feeds\['.*books.jsonl'\] and output both write"):
        _build({"feeds": {path: {}}}, output=path)


def test_the_output_and_the_outlet_reach_the_item_processing(tmp_path: Path) -> None:
    async def outlet(_item: Any) -> None:
        pass

    run = _build({}, output=tmp_path / "books.csv", outlet=outlet)

    assert [feed.path for feed in run.feeds] == [tmp_path / "books.csv"]
    assert run.items._outlet is outlet  # pyright: ignore[reportPrivateUsage]
