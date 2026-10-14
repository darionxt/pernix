from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pernix.addons import setup_addon
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.errors import ComponentError, SettingsError
from pernix.events import StatsSnapshot
from pernix.feeds import Feed, render_path
from pernix.fetchers import FetcherDispatch
from pernix.fingerprint import request_fingerprint
from pernix.frontiers import MemoryFrontier
from pernix.items import ItemProcessor
from pernix.logs import RunContext
from pernix.middlewares import build_download_chain, build_spider_chain
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import (
    CoreSettings,
    DomainSettings,
    FeedOptions,
    FeedSettings,
    FingerprintSettings,
    FrontierSettings,
    ItemSettings,
    RetrySettings,
    StatsSettings,
)
from pernix.stats import CoreStats, StatsReporter
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from pernix.crawler import Crawler
    from pernix.frontiers import Frontier
    from pernix.logs import RunLog
    from pernix.middlewares import Download
    from pernix.request import Request
    from pernix.retry_policy import RetryPolicy
    from pernix.stats import Gauge, StatsSink

__all__ = ["DownloadAssembly", "RunAssembly"]


@dataclass(frozen=True, slots=True)
class DownloadAssembly:
    """The Fetchers and download Middleware of the settings, and the download chain through them.

    A Run fetches through it, and so do single fetches outside a Run, such as those
    of `pernix fetch`.

    Attributes:
        components: The components to subscribe and open, in order: the Fetchers,
            then the download Middleware.
        download: Fetches a Request through the download Middleware and a Fetcher.
    """

    components: tuple[object, ...]
    download: Download

    @classmethod
    def build(cls, crawler: Crawler) -> DownloadAssembly:
        """Build the Fetchers and the download Middleware of the settings, and chain them.

        Args:
            crawler: Builds the components.

        Returns:
            The assembly, not opened yet.

        Raises:
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme.
        """
        fetchers = _build_named(crawler, "fetchers")
        middlewares = _build_all(crawler, "middlewares")
        download = build_download_chain(middlewares, FetcherDispatch(fetchers))
        return cls(components=(*fetchers.values(), *middlewares), download=download)


@dataclass(frozen=True, slots=True)
class RunAssembly:
    """The components of one Run, built and wired together, but not opened yet.

    `build()` constructs everything before anything is opened, so a misconfigured
    component fails the Run before it fetches or writes anything.

    Attributes:
        components: The components to subscribe and open, in order: Stats Sinks,
            Extensions, Fetchers, download Middleware, spider Middleware, Pipelines,
            and failure Pipelines.
        feeds: The Feeds, opened after the components.
        items: Takes the Items through the Pipelines into the Feeds.
        frontier: Holds the Requests of the Run.
        engine: Fetches the Requests and runs their Callbacks.
        reporter: Takes the stats snapshots.
    """

    components: tuple[object, ...]
    feeds: tuple[Feed, ...]
    items: ItemProcessor
    frontier: Frontier
    engine: Engine
    reporter: StatsReporter

    @classmethod
    def build(
        cls,
        crawler: Crawler,
        run_log: RunLog,
        *,
        addons: Sequence[object] = (),
        output: str | os.PathLike[str] | None = None,
        outlet: Callable[[Any], Awaitable[None]] | None = None,
    ) -> RunAssembly:
        """Build the components of a Run from its settings and wire them together.

        Addons register their subscribers first. Then the components are built with
        `crawler.build_component()`: Stats Sinks, Extensions, Fetchers, download
        Middleware, spider Middleware, Pipelines, failure Pipelines, Feeds, the
        Fingerprint function, and the retry policy; the download and spider chains
        are checked. The core stats and the Stats Sinks are subscribed.

        Args:
            crawler: The Crawler of the Run, which builds the components.
            run_log: The log of the Run, whose counts are reported in the stats.
            addons: Loaded Addons whose `setup()` runs first.
            output: A Feed for the Items besides the `feeds` setting.
            outlet: Receives every Item that passes the Pipelines, after the Feeds.

        Returns:
            The assembly.

        Raises:
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme, or a
                Feed is invalid.
        """
        settings, events = crawler.settings, crawler.events
        for addon in addons:
            setup_addon(addon, events=events, settings=settings)
        sinks = _build_sinks(crawler)
        extensions = _build_all(crawler, "extensions")
        downloading = DownloadAssembly.build(crawler)
        spider_middlewares = _build_all(crawler, "spider_middlewares")
        pipelines = _build_all(crawler, "pipelines")
        failed_pipelines = _build_all(crawler, "failed_pipelines")
        feeds = _build_feeds(crawler, output)
        items = _item_processor(crawler, pipelines, failed_pipelines, feeds, outlet)
        core_stats = CoreStats(crawler.stats)
        download = core_stats.count_downloads(downloading.download)
        handle = build_spider_chain(spider_middlewares, run_callbacks(crawler.spider))
        frontier = MemoryFrontier(fingerprint=_fingerprint_function(crawler))
        throttle = Throttle(settings.section(DomainSettings))
        engine = Engine(
            crawler.spider,
            frontier=frontier,
            download=download,
            handle=handle,
            put_item=items.put,
            events=events,
            concurrency=settings.section(CoreSettings).concurrency,
            lease_ttl=settings.section(FrontierSettings).lease_ttl,
            retry_policy=_retry_policy(crawler),
            throttle=throttle,
        )
        events.subscribe_methods(core_stats)
        for sink in sinks:
            events.on(StatsSnapshot, sink.push)
        components = (
            *sinks,
            *extensions,
            *downloading.components,
            *spider_middlewares,
            *pipelines,
            *failed_pipelines,
        )
        return cls(
            components=components,
            feeds=tuple(feeds),
            items=items,
            frontier=frontier,
            engine=engine,
            reporter=_reporter(crawler, frontier, throttle, run_log),
        )


def _build_all(crawler: Crawler, key: str) -> list[object]:
    """Build the enabled components of a `{component: order}` setting, in order."""
    return [crawler.build_component(spec) for spec in crawler.settings.components(key)]


def _build_named(crawler: Crawler, key: str) -> dict[str, object]:
    """Build the enabled components of a `{name: component}` setting."""
    return {name: crawler.build_component(spec) for name, spec in crawler.settings.named_components(key).items()}


def _build_sinks(crawler: Crawler) -> list[StatsSink]:
    """Build the enabled Stats Sinks, checking that each can receive snapshots."""
    sinks = _build_all(crawler, "stats.sinks")
    for sink in sinks:
        if not callable(getattr(sink, "push", None)):
            raise ComponentError(f"stats.sinks: {sink!r} must have push(snapshot)")
    return cast("list[StatsSink]", sinks)


def _build_feeds(crawler: Crawler, output: str | os.PathLike[str] | None) -> list[Feed]:
    """The Feeds of the `feeds` setting and the one `output` names, each writing its own file.

    Raises:
        pernix.errors.SettingsError: A Feed is invalid, or two Feeds, or a Feed and
            `items.failed_output`, would write the same file.
    """
    feed_settings = crawler.settings.section(FeedSettings).feeds
    configured = [(f"feeds[{template!r}]", template, options) for template, options in feed_settings.items()]
    if output is not None:
        configured.append(("output", os.fspath(output), FeedOptions()))
    feeds = [_feed(crawler, template, options) for _, template, options in configured]
    destinations = [(label, feed.path) for (label, _, _), feed in zip(configured, feeds, strict=True)]
    failed_output = crawler.settings.section(ItemSettings).failed_output
    if failed_output is not None:
        destinations.append(("items.failed_output", _feed_path(crawler, failed_output)))
    _check_distinct(destinations)
    return feeds


def _feed(crawler: Crawler, template: str, options: FeedOptions) -> Feed:
    try:
        return Feed(_feed_path(crawler, template), options)
    except (TypeError, ValueError, ComponentError) as exc:
        raise SettingsError([f"feed {template!r}: {exc}"]) from exc


def _feed_path(crawler: Crawler, template: str) -> Path:
    try:
        return render_path(template, name=crawler.spider.name, run_id=crawler.run_id, started=crawler.started_at)
    except (KeyError, IndexError, ValueError) as exc:
        raise SettingsError([f"feed {template!r}: invalid path template: {exc!r}"]) from exc


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


def _item_processor(
    crawler: Crawler,
    pipelines: Sequence[object],
    failed_pipelines: Sequence[object],
    feeds: Sequence[Feed],
    outlet: Callable[[Any], Awaitable[None]] | None,
) -> ItemProcessor:
    """Takes the Items through the Pipelines into the Feeds, as the `items.*` settings say."""
    options = crawler.settings.section(ItemSettings)
    return ItemProcessor(
        pipelines,
        feeds,
        crawler.events,
        crawler.stats,
        failed_pipelines=failed_pipelines,
        outlet=outlet,
        queue_size=options.queue_size,
        concurrency=options.concurrency,
        timeout=options.timeout,
        delay_scale=crawler.settings.section(RetrySettings).delay_scale,
    )


def _fingerprint_function(crawler: Crawler) -> Callable[[Request], str]:
    """The `fingerprint.function` setting, or the default Fingerprint with its options."""
    options = crawler.settings.section(FingerprintSettings)
    if options.function is None:
        return functools.partial(request_fingerprint, headers=options.headers, sort_query=options.sort_query)
    function = crawler.build_component(options.function)
    if not callable(function):
        raise ComponentError(f"fingerprint.function must be callable as function(request) -> str, got {function!r}")
    return cast("Callable[[Request], str]", function)


def _retry_policy(crawler: Crawler) -> RetryPolicy:
    """The `retry.policy` setting, or the default policy, built with injection."""
    spec = crawler.settings.section(RetrySettings).policy
    policy = crawler.build_component(DefaultRetryPolicy if spec is None else spec)
    if not callable(getattr(policy, "decide", None)):
        raise ComponentError(f"retry.policy must have decide(request, outcome, attempt), got {policy!r}")
    return cast("RetryPolicy", policy)


def _reporter(crawler: Crawler, frontier: Frontier, throttle: Throttle, run_log: RunLog) -> StatsReporter:
    """The reporter of the Run's stats, reading the Frontier, the Throttle, and the log counts."""
    gauges: dict[str, Gauge] = {
        "frontier": frontier.counts,
        "throttle": lambda: throttle.stats,
        "log": lambda: run_log.counts,
    }
    interval = crawler.settings.section(StatsSettings).interval
    run = RunContext(crawler.run_id, crawler.spider.name)
    return StatsReporter(crawler.stats, crawler.events, run, interval=interval, gauges=gauges)
