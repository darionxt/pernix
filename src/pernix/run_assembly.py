from __future__ import annotations

import functools
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn, cast

from pernix.addons import setup_addon
from pernix.blocked_responses import BlockedResponses
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.errors import CONFIGURATION_ERRORS, ComponentError, SettingsError
from pernix.events import StatsSnapshot
from pernix.feeds import Feed, render_path
from pernix.fetchers import BrowserSettings, FetcherDispatch, HTTPFetcher, HTTPSettings
from pernix.fingerprint import request_fingerprint
from pernix.frontiers import MemoryFrontier
from pernix.inline_fetch import InlineFetch
from pernix.items import ItemProcessor
from pernix.logs import RunContext
from pernix.middlewares import build_download_chain, build_spider_chain
from pernix.pausing import Pausing
from pernix.proxies import DefaultBindingPolicy, MemoryProxyPool
from pernix.proxies.proxy_sources import ProxyAPI, ProxyFile, StaticProxies
from pernix.proxies.run_proxies import ProxySupply, RunProxies
from pernix.retry_policy import DefaultRetryPolicy
from pernix.sending import IndependentSending
from pernix.session import Session
from pernix.sessions import MemorySessionPool
from pernix.sessions.session_leasing import SessionLeasing
from pernix.sessions.session_login import SessionLogin
from pernix.settings import (
    BlockedSettings,
    CallbackSettings,
    CoreSettings,
    DomainSettings,
    FeedOptions,
    FeedSettings,
    FingerprintSettings,
    FrontierSettings,
    ItemSettings,
    LoopSettings,
    ProxySettings,
    RetrySettings,
    SessionSettings,
    StatsSettings,
)
from pernix.spider_hooks import overrides
from pernix.stats import CoreStats, LoopLag, LoopWatchdog, StatsReporter
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from pernix.account import Account
    from pernix.crawler import Crawler
    from pernix.frontiers import Frontier
    from pernix.logs import RunLog
    from pernix.middlewares import Download
    from pernix.proxies import BindingPolicy, ProxyProvider
    from pernix.request import Request
    from pernix.response import Response
    from pernix.retry_policy import RetryPolicy
    from pernix.spider import Spider
    from pernix.stats import Gauge, Stats, StatsSink

__all__ = ["DownloadAssembly", "RunAssembly"]

logger = logging.getLogger(__name__)


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
            Extensions, the sources of proxies and the proxies, Fetchers, download
            Middleware, spider Middleware, Pipelines, and failure Pipelines.
        feeds: The Feeds, opened after the components.
        items: Takes the Items through the Pipelines into the Feeds.
        frontier: Holds the Requests of the Run.
        engine: Fetches the Requests and runs their Callbacks.
        inline_fetch: Fetches Requests in place, through the same download chain,
            Throttle, and retry policy as the Engine.
        reporter: Takes the stats snapshots.
    """

    components: tuple[object, ...]
    feeds: tuple[Feed, ...]
    items: ItemProcessor
    frontier: Frontier
    engine: Engine
    inline_fetch: InlineFetch
    reporter: StatsReporter

    @classmethod
    def build(
        cls,
        crawler: Crawler,
        run_log: RunLog,
        *,
        output: str | os.PathLike[str] | None = None,
        outlet: Callable[[Any], Awaitable[None]] | None = None,
        accounts: Sequence[Account] = (),
        pausing: Pausing | None = None,
    ) -> RunAssembly:
        """Build the components of a Run from its settings and wire them together.

        The Crawler's Addons register their subscribers first. Then the components
        are built with `crawler.build_component()`: Stats Sinks, Extensions, the
        proxies, their provider, and their binding policy, Fetchers, download
        Middleware, spider Middleware, Pipelines, failure Pipelines, Feeds, the
        Fingerprint function, and the retry policy; the download and spider chains
        and the Pipelines are checked. Steps that do not depend on each other all
        run, so their problems are reported together. The core stats and the Stats
        Sinks are subscribed.

        Args:
            crawler: The Crawler of the Run, which builds the components.
            run_log: The log of the Run, whose counts are reported in the stats.
            output: A Feed for the Items besides the `feeds` setting.
            outlet: Receives every Item that passes the Pipelines, after the Feeds.
            accounts: The Accounts the Spider gave, whose Sessions the Session Pool
                holds, if one is enabled.
            pausing: Pauses and resumes the Run, for the Engine; its counts are
                reported in the stats. Defaults to one that nothing pauses.

        Returns:
            The assembly.

        Raises:
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme, a Feed
                is invalid, or several problems were found; it lists them all.
            pernix.errors.AddonError: An Addon's `setup()` failed.
        """
        settings, events = crawler.settings, crawler.events
        problems = _Problems()
        for addon in crawler.addons:
            problems.attempt(setup_addon, addon, events=events, settings=settings)
        sinks = problems.attempt(_build_sinks, crawler)
        extensions = problems.attempt(_build_all, crawler, "extensions")
        proxying = problems.attempt(_proxying, crawler, run_log)
        downloading = problems.attempt(DownloadAssembly.build, crawler)
        spider_middlewares = problems.attempt(_build_all, crawler, "spider_middlewares")
        pipelines = problems.attempt(_build_all, crawler, "pipelines")
        failed_pipelines = problems.attempt(_build_all, crawler, "failed_pipelines")
        feeds = problems.attempt(_build_feeds, crawler, output)
        fingerprint = problems.attempt(_fingerprint_function, crawler)
        retry_policy = problems.attempt(_retry_policy, crawler)
        handle = None
        if spider_middlewares is not None:
            inherit = _inherits_referer(crawler)
            handle = problems.attempt(
                build_spider_chain,
                spider_middlewares,
                run_callbacks(crawler.spider, inherit_referer=inherit),
                inherit_referer=inherit,
            )
        items = None
        if pipelines is not None and failed_pipelines is not None and feeds is not None:
            items = problems.attempt(_item_processor, crawler, pipelines, failed_pipelines, feeds, outlet)
        if (
            problems.found
            or sinks is None
            or extensions is None
            or proxying is None
            or downloading is None
            or spider_middlewares is None
            or pipelines is None
            or failed_pipelines is None
            or feeds is None
            or fingerprint is None
            or retry_policy is None
            or handle is None
            or items is None
        ):
            problems.raise_them()
        core_stats = CoreStats(crawler.stats)
        pausing = pausing or Pausing(crawler.spider, events)
        frontier = MemoryFrontier(fingerprint=fingerprint)
        throttle = Throttle(settings.section(DomainSettings))
        proxies = proxying.proxies
        blocked = BlockedResponses(crawler.spider.is_blocked, stats=crawler.stats)
        sessions = _session_leasing(
            settings.section(SessionSettings),
            crawler.stats,
            crawler.spider,
            accounts,
            proxies=proxies,
            is_blocked=blocked.blocked,
        )
        sender = sessions or IndependentSending(
            proxies, is_blocked=blocked.blocked, render_blocked=settings.section(BlockedSettings).render
        )
        download = core_stats.count_downloads(downloading.download)
        engine = Engine(
            crawler.spider,
            frontier=frontier,
            download=download,
            handle=handle,
            put_item=items.put,
            events=events,
            concurrency=settings.section(CoreSettings).concurrency,
            browser_concurrency=_browser_settings(crawler).max_pages,
            lease_ttl=settings.section(FrontierSettings).lease_ttl,
            retry_policy=retry_policy,
            throttle=throttle,
            sender=sender,
            callback_timeout=settings.section(CallbackSettings).timeout,
            stats=crawler.stats,
            pausing=pausing,
        )
        inline_fetch = InlineFetch(
            download=download,
            throttle=throttle,
            retry_policy=retry_policy,
            events=events,
            stats=crawler.stats,
            sender=sender,
        )
        events.subscribe_methods(core_stats)
        for sink in sinks:
            events.on(StatsSnapshot, sink.push)
        components = (
            *sinks,
            *extensions,
            *proxying.components,
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
            inline_fetch=inline_fetch,
            reporter=_reporter(crawler, frontier, throttle, run_log, proxying.pool, pausing),
        )


@dataclass(frozen=True, slots=True)
class _Proxying:
    """The Run's proxies, if it has any, their pool, and the components to open for them, in order."""

    proxies: RunProxies | None = None
    pool: MemoryProxyPool | None = None
    components: tuple[object, ...] = ()


def _proxying(crawler: Crawler, run_log: RunLog) -> _Proxying:
    """The proxies of the `proxies.*` settings, from each source set, kept in a pool in this process.

    The API, the provider, and the binding policy are opened before the proxies,
    which load them and bind Sessions with it.

    Raises:
        pernix.errors.SettingsError: A binding policy is set without a source, and
            no Session Pool is enabled, so no Request could have a proxy.
    """
    options = crawler.settings.section(ProxySettings)
    if not options.enabled:
        return _Proxying()
    if not options.sourced and not crawler.settings.section(SessionSettings).enabled:
        raise SettingsError(
            [
                f"{ProxySettings.key('binding')}: without a source of proxies, it gives the Sessions of a Session Pool "
                f"proxies of their own, but no Session Pool is enabled; enable one with {SessionSettings.key('enabled')}"
            ]
        )
    if options.rebind == "never" and options.binding is None and options.refresh is not None:
        logger.warning(
            "%s is never and %s is set: a Session bound to a proxy a refresh no longer lists is retired with it",
            ProxySettings.key("rebind"),
            ProxySettings.key("refresh"),
        )
    sources: list[tuple[str, ProxyProvider]] = []
    opened: list[object] = []
    if options.urls:
        sources.append((ProxySettings.key("urls"), StaticProxies(options.urls)))
    if options.file is not None:
        sources.append((ProxySettings.key("file"), ProxyFile(options.file)))
    if options.api is not None:
        api = ProxyAPI(
            options.api,
            HTTPFetcher(_http_settings(crawler)),
            lifetime=options.api_lifetime,
            answer=options.api_json,
            mask=run_log.mask,
        )
        sources.append((ProxySettings.key("api"), api))
        opened.append(api)
    if options.provider is not None:
        provider = crawler.build_component(options.provider)
        if not callable(getattr(provider, "proxies", None)):
            raise ComponentError(f"proxies.provider must have proxies(), got {provider!r}")
        sources.append((ProxySettings.key("provider"), cast("ProxyProvider", provider)))
        opened.append(provider)
    policy = crawler.build_component(DefaultBindingPolicy if options.binding is None else options.binding)
    if not callable(getattr(policy, "bind", None)):
        raise ComponentError(f"proxies.binding must have bind(binding), got {policy!r}")
    pool = MemoryProxyPool(
        max_sessions=options.max_sessions,
        cooldown=options.cooldown,
        cooldown_max=options.cooldown_max,
        max_failures=options.max_failures,
        stats=crawler.stats,
    )
    proxies = RunProxies(
        pool,
        sources,
        policy=cast("BindingPolicy", policy),
        refresh=options.refresh,
        stats=crawler.stats,
        stop=crawler.stop,
        mask=run_log.mask,
        supply=_supply(options),
    )
    return _Proxying(proxies, pool, (*opened, policy, proxies))


def _supply(options: ProxySettings) -> ProxySupply:
    """How the proxies are supplied: with `proxies.supply = "add"`, added, the API and the provider extracting them."""
    if options.supply != "add":
        return ProxySupply()
    return ProxySupply(
        add=True,
        extracting=frozenset({ProxySettings.key("api"), ProxySettings.key("provider")}),
        min_available=options.min_available,
        interval=options.extract_interval,
        max_extracted=options.max_extracted,
        attempts=options.extract_attempts,
    )


def _http_settings(crawler: Crawler) -> HTTPSettings:
    """The `http.*` settings, or their defaults when no Fetcher registers them."""
    try:
        return crawler.settings.section(HTTPSettings)
    except KeyError:
        return HTTPSettings()


def _browser_settings(crawler: Crawler) -> BrowserSettings:
    """The `browser.*` settings, or their defaults when no Fetcher registers them.

    The Engine runs `browser.max_pages` workers for rendered Requests even then, so
    that they fail as no Fetcher fits them rather than wait forever.
    """
    try:
        return crawler.settings.section(BrowserSettings)
    except KeyError:
        return BrowserSettings()


def _session_leasing(
    settings: SessionSettings,
    stats: Stats,
    spider: Spider,
    accounts: Sequence[Account],
    *,
    proxies: RunProxies | None,
    is_blocked: Callable[[Response], bool],
) -> SessionLeasing | None:
    """The leasing of the Run's Session Pool, held in this process; `None` when Requests are independent.

    With Accounts, the pool holds one Session per Account, under its id, and no
    other. Sessions log in if the Spider defines `login()`, are bound to the Run's
    proxies, if it has any, and are Retired after `sessions.max_blocks` Blocked
    Responses in a row.
    """
    logs_in = overrides(spider.login)
    if not settings.enabled:
        hooks = [f"{name}()" for name in ("accounts", "login") if overrides(getattr(spider, name))]
        if hooks:
            logger.warning(
                "%s defines %s, but no Session Pool is enabled, so no Session is used; enable one with %s",
                type(spider).__name__,
                " and ".join(hooks),
                SessionSettings.key("enabled"),
            )
        return None
    if accounts and not logs_in:
        logger.warning(
            "%s defines no login(), so the Sessions bound to its %d Accounts never log in",
            type(spider).__name__,
            len(accounts),
        )
    pool = MemorySessionPool(
        settings.pool,
        size=len(accounts) or settings.size,
        max_concurrency=settings.max_concurrency,
        lease_ttl=settings.lease_ttl,
        eager=settings.eager and not accounts,
        logs_in=logs_in,
        login_attempts=settings.login_attempts,
        max_blocks=settings.max_blocks,
        stats=stats,
    )
    for account in accounts:
        pool.add(Session(account.id, account=account.id), max_concurrency=account.max_concurrency)
    login = SessionLogin(spider.login, spider.is_logged_out, accounts=accounts) if logs_in else None
    return SessionLeasing(
        pool, settings, stats=stats, login=login, proxies=proxies, accounts=accounts, is_blocked=is_blocked
    )


class _Problems:
    """Collects the configuration errors of independent build steps, to report them together."""

    def __init__(self) -> None:
        self._errors: list[Exception] = []

    @property
    def found(self) -> bool:
        """Whether a step failed."""
        return bool(self._errors)

    def attempt[**P, T](self, step: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T | None:
        """Run a step; on a configuration error, keep it and return `None`."""
        try:
            return step(*args, **kwargs)
        except CONFIGURATION_ERRORS as exc:
            self._errors.append(exc)
            return None

    def raise_them(self) -> NoReturn:
        """Raise the one error found, or a `SettingsError` listing them all, caused by an `ExceptionGroup` of them."""
        if len(self._errors) == 1:
            raise self._errors[0]
        problems = [problem for error in self._errors for problem in getattr(error, "problems", [str(error)])]
        raise SettingsError(problems) from ExceptionGroup("configuration errors", self._errors)


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


def _inherits_referer(crawler: Crawler) -> bool:
    """Whether Requests constructed in Callbacks take the page they come from as their `Referer` (`http.inherit_referer`)."""
    if HTTPSettings not in crawler.settings.sections():  # the http Fetcher is another component's
        return False
    return crawler.settings.section(HTTPSettings).inherit_referer


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


def _reporter(
    crawler: Crawler,
    frontier: Frontier,
    throttle: Throttle,
    run_log: RunLog,
    proxies: MemoryProxyPool | None,
    pausing: Pausing,
) -> StatsReporter:
    """The reporter of the Run's stats, reading the pauses, the Frontier, the Throttle, the proxies, and the log counts.

    It samples the event loop lag, which beats for a watchdog of the loop, unless
    `loop.block_threshold` is 0.
    """
    gauges: dict[str, Gauge] = {
        "run": pausing.counts,
        "frontier": frontier.counts,
        "throttle": lambda: throttle.stats,
        "log": lambda: run_log.counts,
    }
    if proxies is not None:
        gauges["proxies"] = proxies.counts
    interval = crawler.settings.section(StatsSettings).interval
    run = RunContext(crawler.run_id, crawler.spider.name)
    threshold = crawler.settings.section(LoopSettings).block_threshold
    lag = LoopLag(watchdog=LoopWatchdog(threshold, stats=crawler.stats) if threshold else None)
    return StatsReporter(crawler.stats, crawler.events, run, interval=interval, gauges=gauges, lag=lag)
