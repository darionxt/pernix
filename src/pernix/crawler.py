from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from pernix.account import collect_accounts
from pernix.addons import addon_settings, load_addons
from pernix.component_lifecycle import ComponentLifecycle
from pernix.components import build_component
from pernix.errors import CONFIGURATION_ERRORS
from pernix.events import EventBus, SpiderClosed, SpiderOpened
from pernix.items import ItemStream
from pernix.logs import RunContext, RunLog, revealed_secrets, run_context
from pernix.pausing import Pausing
from pernix.request import Request
from pernix.run_assembly import RunAssembly
from pernix.run_result import RunResult
from pernix.settings import (
    CoreSettings,
    EventSettings,
    LogSettings,
    Section,
    SessionSettings,
    Settings,
    ShutdownSettings,
    load_settings,
)
from pernix.shutdown import Shutdown
from pernix.spider_arguments import create_spider
from pernix.stats import Stats

if TYPE_CHECKING:
    import os
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from pernix.account import Account
    from pernix.engine import Engine
    from pernix.feeds import Feed
    from pernix.fetchers import StreamedResponse
    from pernix.inline_fetch import InlineFetch
    from pernix.items import ItemProcessor
    from pernix.response import Response
    from pernix.shutdown import StopMode
    from pernix.spider import Spider

__all__ = ["Crawler"]

logger = logging.getLogger(__name__)

_REASONS: dict[StopMode | None, str] = {None: "finished", "drain": "drained", "force": "forced"}


class Crawler:
    """Runs one Run of a Spider: builds its components, opens them, runs it, and stops it.

    `pernix.run_assembly.RunAssembly` decides which components a Run has and wires
    them together; the Crawler builds each one with injection, carries the Run
    through its lifecycle, and carries out `stop()`.

    Attributes:
        spider: The Spider being run; the Crawler binds itself as its `crawler`.
        settings: The validated, immutable settings of this Run.
        events: The event bus of this Run.
        run_id: Identifies the Run: the `run_id` setting, or a generated one.
        started_at: When the Run started, in UTC; until then, when the Crawler was
            created. Feed paths fill in `{time}` with it.
        stats: The stats of this Run; they stay readable after it ends.
        addons: The loaded Addons, whose `setup()` runs before the Run starts.
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
        self.addons = tuple(addons)
        self._started = False
        shutdown = settings.section(ShutdownSettings)
        self._close_timeout = shutdown.close_timeout or None
        self._shutdown = Shutdown(grace=shutdown.grace, close_timeout=self._close_timeout)
        self._pausing = Pausing(spider, self.events)
        self._outlet: Callable[[Any], Awaitable[None]] | None = None
        self._inline_fetch: InlineFetch | None = None

    @classmethod
    def from_spider(
        cls,
        spider_type: type[Spider],
        *,
        args: Mapping[str, object] | None = None,
        settings: Mapping[str, Any] | None = None,
        command_line: Mapping[str, str] | None = None,
        output: str | os.PathLike[str] | None = None,
    ) -> Crawler:
        """Load the settings, including enabled Addons, create the Spider, and create a Crawler.

        The `addons` setting is read first, ignoring keys that only the Addons'
        components declare; the settings are then loaded again with each Addon's
        layer. The Spider is created with its arguments, converted as
        `pernix.spider_arguments.spider_arguments()` describes.

        Args:
            spider_type: The Spider class; a new object is created for the Run.
            args: The Spider arguments, by name; strings are converted to the
                annotated types.
            settings: Settings for this Run, above every layer but the command line.
            command_line: Settings given as text on the command line, parsed like
                environment variables; the highest layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.

        Returns:
            The Crawler, not yet started.

        Raises:
            pernix.errors.SettingsError: The settings are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
            pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
        """
        enabled = load_settings(
            sections=(CoreSettings,),
            spider_settings=spider_type.settings,
            overrides=settings,
            command_line=command_line,
            ignore_unknown=True,
        )
        addons = load_addons(enabled.section(CoreSettings).addons)
        final = load_settings(
            addon_layers=[addon_settings(addon) for addon in addons],
            spider_settings=spider_type.settings,
            overrides=settings,
            command_line=command_line,
        )
        return cls(create_spider(spider_type, args or {}), settings=final, output=output, addons=addons)

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

    def stop(self, mode: StopMode = "drain") -> None:
        """Ask the Run to stop; it returns at once.

        A Drain (`"drain"`) stops claiming new Requests and scheduling those of
        `start()` and `on_idle()`, and finishes the Requests in flight and the queued
        Items within `shutdown.grace` seconds; after that, it turns into a Force Stop.
        The Requests the Callbacks yield meanwhile still enter the Frontier. A Force
        Stop (`"force"`) cancels the Requests in flight and returns them to the
        Frontier without counting an attempt, discards the Items not processed yet,
        counting them as `items.discarded`, and closes the components, each within
        `shutdown.close_timeout` seconds. The Feeds are closed either way, keeping
        the Items written. A Force Stop while the Spider's Accounts are imported, or
        while the components open, cancels that and ends the Run before it starts,
        closing the components already open; one while they close limits the close
        in progress too.

        It may be called from a Callback, a component, or another task, and before
        the Run starts. A Force Stop requested during a Drain escalates it; anything
        else requested again, and anything requested after the Run, does nothing.

        Args:
            mode: `"drain"` or `"force"`.

        Raises:
            ValueError: `mode` is neither.
        """
        self._shutdown.request(mode)

    @property
    def paused(self) -> bool:
        """Whether the Run is paused, or will start paused; `False` once it is stopping or over."""
        return self._shutdown.mode is None and self._pausing.paused

    def pause(self) -> None:
        """Pause the Run: stop claiming new Requests until `resume()`; it returns at once.

        The Requests in flight finish, with their Callbacks, Inline Fetches, and Items,
        and are settled as usual, so a paused Run holds no Lease once they are done.
        No Request is taken from `start()` or `on_idle()` meanwhile, though the one
        being produced still enters the Frontier, and the Run is not idle, so it does
        not end on its own; a Drain or a Force Stop still ends it. Lease renewals,
        Cooldowns, proxy refreshes, and stats snapshots go on. Pausing is logged and
        emitted as `SpiderPaused`, and the time spent paused is counted in
        `run.pauses` and `run.paused_seconds`.

        It may be called from a Callback, a component, or another task. Called before
        the Run starts, the Run starts paused, emitting `SpiderPaused` after
        `SpiderOpened`. Called again while paused, once a stop was requested, or after
        the Run, it does nothing.
        """
        if self._shutdown.mode is None:
            self._pausing.pause()

    def resume(self) -> None:
        """Resume a paused Run: claim Requests again; it returns at once.

        Resuming is logged and emitted as `SpiderResumed`. Called while not paused,
        once a stop was requested, or after the Run, it does nothing.
        """
        if self._shutdown.mode is None:
            self._pausing.resume()

    async def fetch(self, request: Request | str) -> Response:
        """Fetch a Request in place and return its Response: an Inline Fetch.

        The Request goes through the download Middleware and waits for its slot, as
        Requests from the Frontier do, but it is not deduplicated or stored, and no
        Callback runs for it. Nested in another fetch for the same domain, in the
        same task, such as from a download Middleware, it shares that fetch's place
        in the slot, though it still waits for the delay, the rate, and a cooldown. The retry policy judges it as usual, and a retry waits
        in place. A Request without a Session continues the Session of the fetch it
        is nested in, or of the Request whose Callback or errback is running;
        otherwise, as any Request, it leases one when a Session Pool is enabled, and
        without one it sends only the cookies it carries. Cancelling the caller, as a Force Stop does,
        cancels the fetch.

        It works while the Run runs, from `SpiderOpened` to `SpiderClosed`: in
        `start()`, `on_idle()`, Callbacks, errbacks, and components.

        Args:
            request: The Request, or a URL to GET; a relative URL resolves against the
                Response whose Callback is running.

        Returns:
            The Response, whatever its status code, unless the retry policy retries it.

        Raises:
            RuntimeError: The Run is not running.
            pernix.errors.RetriesExhausted: The policy retried the Request until no
                retry was left; it holds the last Response, or is caused by the last
                exception.
            pernix.errors.FetchError: The fetch failed in a way the policy does not
                retry.
            pernix.errors.IgnoreRequest: A download Middleware dropped the Request.

        Example:
            >>> async def parse(self, response):
            ...     api = await self.crawler.fetch(f"/api/comments?id={response.meta['id']}")
            ...     yield {"comments": api.json()["data"]}
        """
        return await self._running_inline_fetch().fetch(_as_request(request))

    def stream(
        self, request: Request | str, *, max_bytes: int | None = None
    ) -> AbstractAsyncContextManager[StreamedResponse]:
        """Fetch a Request in place, reading its body as it arrives, such as to download a file.

        As `fetch()`, but the body is read once from the `StreamedResponse`, with
        `iter_bytes()` or `save()`. A slow reader holds about a megabyte of it in
        memory at most, as the Fetcher stops receiving until the reader catches up.
        Only the attempts up to the response headers are retried, and the stream
        holds its slot until it is closed; the fetches made in the block, in the same
        task, share it.

        Download Middleware see the Response marked `streamed`, without its body. A
        Middleware may return another Response, such as a cached one, whose body is
        then read instead. A Fetcher without `stream()` reads the whole body first.

        Args:
            request: The Request, or a URL to GET.
            max_bytes: Bytes the body may have at most; `None` for the Fetcher's own
                limit, such as `http.max_body_size`, and 0 for no limit.

        Returns:
            An async context manager giving the `StreamedResponse`; the transfer is
            closed on exit.

        Raises:
            RuntimeError: The Run is not running.

        Example:
            >>> async def parse(self, response):
            ...     async with self.crawler.stream(response.css("a.pdf::attr(href)").get()) as pdf:
            ...         await pdf.save("report.pdf")
        """
        return self._running_inline_fetch().stream(_as_request(request), max_bytes=max_bytes)

    def stream_items(self) -> ItemStream:
        """Run the Spider while streaming the Items that pass every Pipeline.

        The Run starts with the first iteration; see `pernix.items.ItemStream`.

        Returns:
            The Items, as an async iterator and an async context manager.

        Raises:
            RuntimeError: This Crawler already ran or streams already.
        """
        if self._started or self._outlet is not None:
            raise RuntimeError("a Crawler runs only once; create a new one for another Run")
        channel: asyncio.Queue[Any] = asyncio.Queue(1)
        self._outlet = channel.put
        return ItemStream(self.crawl, self.stop, channel)

    async def crawl(self) -> RunResult:
        """Run the Spider to completion.

        The `pernix.*` log records of the Run are delivered through a `RunLog`, and
        carry its `run_id`. With a Session Pool, the Spider's Accounts are imported
        first, with `Spider.accounts()`. Every component is built and wired, as
        `pernix.run_assembly.RunAssembly.build()` describes, before anything is
        opened. The components are subscribed and opened in order, then the Feeds,
        and `SpiderOpened` is emitted. The Engine
        runs with an in-memory Frontier while Items go through the Pipelines into the
        Feeds and the stats are reported; once it is done, the queued Items and the
        pending batches are processed. Then `SpiderClosed` is emitted, the final stats
        snapshot is taken, and the components are closed in reverse order, which
        finishes the Feed files. `stop()` ends the Run early, and `pause()` holds it
        until `resume()`.

        An error that ends the Run is logged, with its traceback unless it is a
        configuration error, and raised.

        Returns:
            How the Run ended.

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
                try:
                    return await self._crawl(run_log)
                except CONFIGURATION_ERRORS as exc:
                    logger.error("%s", exc)
                    raise
                except Exception as exc:
                    logger.error("The Run failed", exc_info=exc)
                    raise

    async def _crawl(self, run_log: RunLog) -> RunResult:
        try:
            async with ComponentLifecycle() as lifecycle:
                self._shutdown.begin(lifecycle)
                accounts: tuple[Account, ...] = ()

                async def import_accounts() -> None:
                    nonlocal accounts
                    accounts = await self._accounts(run_log)

                # The Accounts come before the components are built, as they make up the Session Pool.
                if await self._shutdown.run_stage(import_accounts()):
                    return self._forced_before_start()
                run = RunAssembly.build(
                    self, run_log, output=self._output, outlet=self._outlet, accounts=accounts, pausing=self._pausing
                )
                self._shutdown.attach(run.engine)
                try:
                    if await self._shutdown.run_stage(self._open(lifecycle, run.components, run.feeds)):
                        return self._forced_before_start()
                    return await self._run(run)
                except asyncio.CancelledError:
                    lifecycle.limit_closing(self._close_timeout)
                    raise
                finally:
                    await run.frontier.close()
        finally:
            self._shutdown.end()

    def _forced_before_start(self) -> RunResult:
        self.stats.set("run.reason", "forced")  # as the reporter records it once a Run started
        return RunResult(self.run_id, "forced", self.stats.snapshot())

    async def _accounts(self, run_log: RunLog) -> tuple[Account, ...]:
        """The Accounts the Spider gives, their credentials masked in the log from now on; none without a Session Pool."""
        if not self.settings.section(SessionSettings).enabled:
            return ()
        accounts = await collect_accounts(self.spider.accounts())
        run_log.mask(revealed_secrets(accounts))
        return accounts

    async def _open(self, lifecycle: ComponentLifecycle, components: Sequence[object], feeds: Sequence[Feed]) -> None:
        """Subscribe and open the components, then open the Feeds."""
        for component in components:
            self.events.subscribe_methods(component)
            await lifecycle.open(component)
        for feed in feeds:
            await lifecycle.open(feed)

    async def _run(self, run: RunAssembly) -> RunResult:
        reason = "failed"
        run.reporter.start()
        self._inline_fetch = run.inline_fetch
        try:
            try:
                await self.events.emit(SpiderOpened(self.spider))
                reason = await self._work(run.engine, run.items)
            except asyncio.CancelledError:
                reason = "cancelled"
                raise
            finally:
                await self.events.emit(SpiderClosed(self.spider, reason))
        finally:
            self._inline_fetch = None
            snapshot = await run.reporter.finish(reason)
        return RunResult(self.run_id, reason, snapshot)

    def _running_inline_fetch(self) -> InlineFetch:
        if self._inline_fetch is None:
            raise RuntimeError(
                "Inline Fetches work only while the Run runs, from SpiderOpened to SpiderClosed: "
                "in start(), on_idle(), Callbacks, errbacks, and components"
            )
        return self._inline_fetch

    async def _work(self, engine: Engine, items: ItemProcessor) -> str:
        """Fetch and process the Items until done, or until stopped; return how the work ended."""
        try:
            await self._shutdown.run_stage(self._fetch_and_process(engine, items))
        finally:
            items.discard()
        return _REASONS[self._shutdown.mode]

    @staticmethod
    async def _fetch_and_process(engine: Engine, items: ItemProcessor) -> None:
        async with asyncio.TaskGroup() as tasks:
            items.start(tasks)
            await engine.run()
            await items.join()

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


def _as_request(request: Request | str) -> Request:
    return Request(request) if isinstance(request, str) else request
