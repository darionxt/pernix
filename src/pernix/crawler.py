from __future__ import annotations

import asyncio
import datetime as dt
import functools
import os
import uuid
from typing import TYPE_CHECKING, Any, cast

from pernix.addons import addon_settings, load_addons, setup_addon
from pernix.callbacks import run_callbacks
from pernix.component_lifecycle import ComponentLifecycle
from pernix.components import build_component
from pernix.engine import Engine
from pernix.errors import ComponentError, SettingsError
from pernix.events import EventBus, SpiderClosed, SpiderOpened, StatsSnapshot
from pernix.feeds import Feed, render_path
from pernix.fetchers import FetcherDispatch
from pernix.fingerprint import request_fingerprint
from pernix.frontiers import MemoryFrontier
from pernix.items import ItemProcessor
from pernix.logs import RunContext, RunLog, revealed_secrets, run_context
from pernix.middlewares import build_download_chain, build_spider_chain
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import (
    CoreSettings,
    DomainSettings,
    EventSettings,
    FeedOptions,
    FeedSettings,
    FingerprintSettings,
    FrontierSettings,
    ItemSettings,
    LogSettings,
    RetrySettings,
    Section,
    Settings,
    StatsSettings,
    load_settings,
)
from pernix.stats import CoreStats, Stats, StatsReporter
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from pernix.frontiers import Frontier
    from pernix.request import Request
    from pernix.retry_policy import RetryPolicy
    from pernix.spider import Spider
    from pernix.stats import Gauge, StatsSink

__all__ = ["Crawler"]


class Crawler:
    """Assembles the components for one Run of a Spider and runs it.

    Attributes:
        spider: The Spider being run; the Crawler binds itself as its `crawler`.
        settings: The validated, immutable settings of this Run.
        events: The event bus of this Run.
        run_id: Identifies the Run: the `run_id` setting, or a generated one.
        started_at: When the Run started, in UTC; until then, when the Crawler was
            created. Feed paths fill in `{time}` with it.
        stats: The stats of this Run; they stay readable after it ends.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        settings: Settings,
        output: str | os.PathLike[str] | None = None,
        addons: Sequence[object] = (),
    ) -> None:
        """Configure a Run. `from_spider()` also loads the settings and Addons.

        Args:
            spider: The Spider to run.
            settings: Settings created by `pernix.settings.load_settings()`.
            output: A Feed for the Items besides the `feeds` setting, as a path
                template whose extension names the format: `.jsonl`, `.csv`, or
                `.json`.
            addons: Loaded Addons whose `setup()` runs before the Run starts.

        Raises:
            RuntimeError: The Spider object is bound to another Crawler.
        """
        self.spider = spider
        self.settings = settings
        self.events = EventBus(timeout=settings.section(EventSettings).timeout)
        self.run_id = settings.section(CoreSettings).run_id or uuid.uuid4().hex
        self.started_at = dt.datetime.now(dt.UTC)
        self.stats = Stats()
        spider.crawler = self
        self._run_context = RunContext(self.run_id, spider.name)
        self._output = output
        self._addons = tuple(addons)
        self._started = False

    @classmethod
    def from_spider(
        cls,
        spider_type: type[Spider],
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
    ) -> Crawler:
        """Load the settings, including enabled Addons, and create a Crawler.

        The `addons` setting is read first, ignoring keys that only the Addons'
        components declare; the settings are then loaded again with each Addon's
        layer.

        Args:
            spider_type: The Spider class; a new object is created for the Run.
            settings: Run arguments, the highest settings layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.

        Returns:
            The Crawler, not yet started.

        Raises:
            pernix.errors.SettingsError: The settings are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
        """
        enabled = load_settings(
            sections=(CoreSettings,), spider_settings=spider_type.settings, overrides=settings, ignore_unknown=True
        )
        addons = load_addons(enabled.section(CoreSettings).addons)
        final = load_settings(
            addon_layers=[addon_settings(addon) for addon in addons],
            spider_settings=spider_type.settings,
            overrides=settings,
        )
        return cls(spider_type(), settings=final, output=output, addons=addons)

    def build_component(self, spec: object) -> object:
        """Construct a component for this Run.

        Objects are used as is; classes are built through `from_crawler(crawler)` if
        they define it, and otherwise get their `__init__` parameters annotated with
        exactly `Crawler`, `Settings`, `EventBus`, `Stats`, or a registered Section
        injected.

        Args:
            spec: A class, an object, or an import path.

        Returns:
            The component.

        Raises:
            pernix.errors.ComponentError: The component cannot be loaded or built.
        """
        return build_component(spec, crawler=self, provide=self._provide, known_types=self._service_types())

    async def crawl(self) -> None:
        """Run the Spider to completion.

        The `pernix.*` log records of the Run are delivered through a `RunLog`, and
        carry its `run_id`. Addons register their subscribers. Then every component
        is built: Stats Sinks, Extensions, Fetchers, download Middleware, spider
        Middleware, Pipelines, failure Pipelines, Feeds, the Fingerprint function, and
        the retry policy,
        and the chains are checked, before anything is opened. The components are
        subscribed and opened in that order, and `SpiderOpened` is emitted. The Engine
        runs with an in-memory Frontier while Items go through the Pipelines into the
        Feeds and the stats are reported; once it is done, the queued Items and the
        pending batches are processed. Then `SpiderClosed` is emitted, the final stats
        snapshot is taken, and the components are closed in reverse order, which
        finishes the Feed files.

        Raises:
            RuntimeError: This Crawler already ran.
            ExceptionGroup: Writing an Item to a Feed failed; it holds the error.
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme, or a
                Feed is invalid.
        """
        if self._started:
            raise RuntimeError("a Crawler runs only once; create a new one for another Run")
        self._started = True
        self.started_at = dt.datetime.now(dt.UTC)
        secrets = revealed_secrets(self.settings.values())
        with run_context(self._run_context):
            async with RunLog(self.settings.section(LogSettings), self._run_context, secrets=secrets) as run_log:
                await self._crawl(run_log)

    async def _crawl(self, run_log: RunLog) -> None:
        for addon in self._addons:
            setup_addon(addon, events=self.events, settings=self.settings)
        sinks = self._build_sinks()
        extensions = self._build_all("extensions")
        fetchers = self._build_named("fetchers")
        middlewares = self._build_all("middlewares")
        spider_middlewares = self._build_all("spider_middlewares")
        pipelines = self._build_all("pipelines")
        failed_pipelines = self._build_all("failed_pipelines")
        feeds = self._build_feeds()
        items = self._item_processor(pipelines, failed_pipelines, feeds)
        core_stats = CoreStats(self.stats)
        download = core_stats.count_downloads(build_download_chain(middlewares, FetcherDispatch(fetchers)))
        handle = build_spider_chain(spider_middlewares, run_callbacks(self.spider))
        frontier = MemoryFrontier(fingerprint=self._fingerprint_function())
        retry_policy = self._retry_policy()
        throttle = Throttle(self.settings.section(DomainSettings))
        reporter = self._reporter(frontier, throttle, run_log)
        self.events.subscribe_methods(core_stats)
        for sink in sinks:
            self.events.on(StatsSnapshot, sink.push)
        components = (
            *sinks,
            *extensions,
            *fetchers.values(),
            *middlewares,
            *spider_middlewares,
            *pipelines,
            *failed_pipelines,
        )
        async with ComponentLifecycle() as lifecycle:
            for component in components:
                self.events.subscribe_methods(component)
                await lifecycle.open(component)
            for feed in feeds:
                await lifecycle.open(feed)
            try:
                engine = Engine(
                    self.spider,
                    frontier=frontier,
                    download=download,
                    handle=handle,
                    put_item=items.put,
                    events=self.events,
                    concurrency=self.settings.section(CoreSettings).concurrency,
                    lease_ttl=self.settings.section(FrontierSettings).lease_ttl,
                    retry_policy=retry_policy,
                    throttle=throttle,
                )
                await self._run(engine, items, reporter)
            finally:
                await frontier.close()

    def _build_all(self, key: str) -> list[object]:
        """Build the enabled components of a `{component: order}` setting, in order."""
        return [self.build_component(spec) for spec in self.settings.components(key)]

    def _build_sinks(self) -> list[StatsSink]:
        """Build the enabled Stats Sinks, checking that each can receive snapshots."""
        sinks = self._build_all("stats.sinks")
        for sink in sinks:
            if not callable(getattr(sink, "push", None)):
                raise ComponentError(f"stats.sinks: {sink!r} must have push(snapshot)")
        return cast("list[StatsSink]", sinks)

    def _reporter(self, frontier: Frontier, throttle: Throttle, run_log: RunLog) -> StatsReporter:
        """The reporter of this Run's stats, reading the Frontier, the Throttle, and the log counts."""
        gauges: dict[str, Gauge] = {
            "frontier": frontier.counts,
            "throttle": lambda: throttle.stats,
            "log": lambda: run_log.counts,
        }
        interval = self.settings.section(StatsSettings).interval
        return StatsReporter(self.stats, self.events, self._run_context, interval=interval, gauges=gauges)

    def _build_feeds(self) -> list[Feed]:
        """The Feeds of the `feeds` setting and the one `output` names, each writing its own file.

        Raises:
            pernix.errors.SettingsError: A Feed is invalid, or two Feeds, or a Feed
                and `items.failed_output`, would write the same file.
        """
        feed_settings = self.settings.section(FeedSettings).feeds
        configured = [(f"feeds[{template!r}]", template, options) for template, options in feed_settings.items()]
        if self._output is not None:
            configured.append(("output", os.fspath(self._output), FeedOptions()))
        feeds = [self._feed(template, options) for _, template, options in configured]
        destinations = [(label, feed.path) for (label, _, _), feed in zip(configured, feeds, strict=True)]
        failed_output = self.settings.section(ItemSettings).failed_output
        if failed_output is not None:
            destinations.append(("items.failed_output", self._feed_path(failed_output)))
        _check_distinct(destinations)
        return feeds

    def _feed(self, template: str, options: FeedOptions) -> Feed:
        try:
            return Feed(self._feed_path(template), options)
        except (TypeError, ValueError, ComponentError) as exc:
            raise SettingsError([f"feed {template!r}: {exc}"]) from exc

    def _feed_path(self, template: str) -> Path:
        try:
            return render_path(template, name=self.spider.name, run_id=self.run_id, started=self.started_at)
        except (KeyError, IndexError, ValueError) as exc:
            raise SettingsError([f"feed {template!r}: invalid path template: {exc!r}"]) from exc

    def _item_processor(
        self, pipelines: Sequence[object], failed_pipelines: Sequence[object], feeds: Sequence[Feed]
    ) -> ItemProcessor:
        """Takes the Items through the Pipelines into the Feeds, as the `items.*` settings say."""
        options = self.settings.section(ItemSettings)
        return ItemProcessor(
            pipelines,
            feeds,
            self.events,
            self.stats,
            failed_pipelines=failed_pipelines,
            queue_size=options.queue_size,
            concurrency=options.concurrency,
            timeout=options.timeout,
            delay_scale=self.settings.section(RetrySettings).delay_scale,
        )

    def _build_named(self, key: str) -> dict[str, object]:
        """Build the enabled components of a `{name: component}` setting."""
        return {name: self.build_component(spec) for name, spec in self.settings.named_components(key).items()}

    def _fingerprint_function(self) -> Callable[[Request], str]:
        """The `fingerprint.function` setting, or the default Fingerprint with its options."""
        options = self.settings.section(FingerprintSettings)
        if options.function is None:
            return functools.partial(request_fingerprint, headers=options.headers, sort_query=options.sort_query)
        function = self.build_component(options.function)
        if not callable(function):
            raise ComponentError(f"fingerprint.function must be callable as function(request) -> str, got {function!r}")
        return cast("Callable[[Request], str]", function)

    def _retry_policy(self) -> RetryPolicy:
        """The `retry.policy` setting, or the default policy, built with injection."""
        spec = self.settings.section(RetrySettings).policy
        policy = self.build_component(DefaultRetryPolicy if spec is None else spec)
        if not callable(getattr(policy, "decide", None)):
            raise ComponentError(f"retry.policy must have decide(request, outcome, attempt), got {policy!r}")
        return cast("RetryPolicy", policy)

    async def _run(self, engine: Engine, items: ItemProcessor, reporter: StatsReporter) -> None:
        reason = "failed"
        reporter.start()
        try:
            try:
                await self.events.emit(SpiderOpened(self.spider))
                async with asyncio.TaskGroup() as tasks:
                    items.start(tasks)
                    await engine.run()
                    await items.join()
                reason = "finished"
            except asyncio.CancelledError:
                reason = "cancelled"
                raise
            finally:
                await self.events.emit(SpiderClosed(self.spider, reason))
        finally:
            await reporter.finish(reason)

    def _service_types(self) -> dict[str, type]:
        sections = {section.__name__: section for section in self.settings.sections()}
        return {**sections, "Crawler": Crawler, "Settings": Settings, "EventBus": EventBus, "Stats": Stats}

    def _provide(self, service_type: type) -> object | None:
        # Match annotations exactly: isinstance() would fail on Any and on Protocols
        # that are not runtime-checkable, and would inject for loose types like object.
        if service_type in (Crawler, type(self)):
            return self
        if service_type is Settings:
            return self.settings
        if service_type is EventBus:
            return self.events
        if service_type is Stats:
            return self.stats
        if issubclass(service_type, Section):
            try:
                return self.settings.section(service_type)
            except KeyError:
                return None
        return None


def _check_distinct(destinations: Sequence[tuple[str, Path]]) -> None:
    """Fail when two outputs would write the same file, as the last one closed would replace the others."""
    seen: dict[Path, str] = {}
    problems: list[str] = []
    for label, path in destinations:
        key = path.resolve()
        if key in seen:
            problems.append(f"{seen[key]} and {label} both write {path}")
        else:
            seen[key] = label
    if problems:
        raise SettingsError(problems)
