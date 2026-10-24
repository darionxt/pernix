from __future__ import annotations

import asyncio
import datetime as dt
import functools
import logging
import uuid
from contextlib import aclosing
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from pernix.account import collect_accounts
from pernix.addons import addon_settings, load_addons
from pernix.callbacks import iterate_and_close
from pernix.component_lifecycle import ComponentLifecycle
from pernix.components import build_component
from pernix.errors import CONFIGURATION_ERRORS, FeedError, JobDirError, SerializationError, SettingsError
from pernix.events import EventBus, SpiderClosed, SpiderOpened
from pernix.feeds import Feed, render_path
from pernix.frontiers import SQLiteFrontier
from pernix.items import ItemStream
from pernix.job_dir import JobDir, RunRecord
from pernix.logs import RunContext, RunLog, revealed_secrets, run_context
from pernix.outcome_file import OutcomeFile
from pernix.pausing import Pausing
from pernix.request import Request
from pernix.run_assembly import RunAssembly, outcome_path
from pernix.run_result import RunResult
from pernix.settings import (
    CoreSettings,
    EventSettings,
    FeedOptions,
    FingerprintSettings,
    LogSettings,
    Section,
    SessionSettings,
    Settings,
    ShutdownSettings,
    load_settings,
)
from pernix.shutdown import Shutdown
from pernix.spider_arguments import create_spider, spider_arguments
from pernix.stats import Stats

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager
    from signal import Signals

    from pernix.account import Account
    from pernix.engine import Engine
    from pernix.fetchers import StreamedResponse
    from pernix.inline_fetch import InlineFetch
    from pernix.items import ItemProcessor
    from pernix.items.item_processor import Outlet
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

    With the `job_dir` setting, the Run keeps its Frontier in that directory, with a
    record of itself, so that running it again resumes it: see `crawl()`.

    Attributes:
        spider: The Spider being run; the Crawler binds itself as its `crawler`.
        settings: The validated, immutable settings of this Run.
        events: The event bus of this Run.
        run_id: Identifies the Run: the `run_id` setting, the one the job directory
            records, or a generated one.
        started_at: When the Run started, in UTC, in its first process for a resumed
            Run; until then, when the Crawler was created. Feed paths fill in
            `{time}` with it.
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
        arguments: Mapping[str, object] | None = None,
    ) -> None:
        """Configure a Run. `from_spider()` also loads the settings and Addons.

        With the `job_dir` setting, the Run recorded there, if any, gives the
        `run_id`, unless the setting names one, and the time the Run started.

        Args:
            spider: The Spider to run.
            settings: Settings created by `pernix.settings.load_settings()`.
            output: A Feed for the Items besides the `feeds` setting, as a path
                template whose extension names the format: `.jsonl`, `.csv`, or
                `.json`.
            addons: Loaded Addons whose `setup()` runs before the Run starts.
            arguments: The Spider arguments the Spider was created with, as
                converted for it, which a job directory records; `None` when not
                known.

        Raises:
            RuntimeError: The Spider object is bound to another Crawler.
        """
        self.spider = spider
        self.settings = settings
        self.events = EventBus(timeout=settings.section(EventSettings).timeout)
        core = settings.section(CoreSettings)
        self._job = None if core.job_dir is None else JobDir(core.job_dir)
        self._job_taken = False
        recorded = None if self._job is None else self._job.peek()
        self.run_id = core.run_id or (None if recorded is None else recorded.run_id) or uuid.uuid4().hex
        self.started_at = dt.datetime.now(dt.UTC) if recorded is None else recorded.started_at
        self._resuming = recorded is not None
        self._arguments = None if arguments is None else dict(arguments)
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
        self._outlet: Outlet | None = None
        self._inline_fetch: InlineFetch | None = None
        self._interrupted = False

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
        arguments = spider_arguments(spider_type, args or {})
        spider = create_spider(spider_type, arguments)
        return cls(spider, settings=final, output=output, addons=addons, arguments=arguments)

    def feed(self, template: str, options: FeedOptions | None = None) -> Feed:
        """Create a Feed of this Run, writing the file a path template names.

        The template's `{name}`, `{run_id}`, and `{time}` are filled in. Without the
        `job_dir` setting, the Feed writes its file atomically, a temporary file
        renamed over it once closed. With it, the Feed writes its file in place,
        each Item flushed as it is written, and the job directory records the file
        once the Feed has begun it: a resumed Run continues the files its earlier
        processes began, and replaces any other, as a new Run does. See
        `pernix.feeds.Feed`.

        Args:
            template: The path template.
            options: The format, fields, accepted Items, and encoding.

        Returns:
            The Feed, not yet open.

        Raises:
            ValueError: The template is invalid, the format cannot be inferred, the
                encoding does not allow writing in place, or the file is one the job
                directory keeps.
            TypeError: `options.accepts` is neither types nor a function.
            pernix.errors.FeedError: An earlier process of the Run wrote the file in
                another format.
            RuntimeError: The Run has not taken its job directory yet, as before it
                starts.
        """
        try:
            path = render_path(template, name=self.spider.name, run_id=self.run_id, started=self.started_at)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"invalid path template: {exc!r}") from exc
        if self._job is None:
            return Feed(path, options)
        if _within(path, self._job.path) and any(path.resolve() == kept.resolve() for kept in self._job.files):
            raise ValueError(f"{path} is a file the job directory keeps")
        recorded = self._job.recorded_feed(path)
        if recorded is None:
            return _RecordedFeed(path, options, job=self._job)
        feed = Feed(path, options, writing="continued")
        if feed.format != recorded:
            raise FeedError(
                f"cannot continue the feed {path}: the Run wrote it as {recorded} before, not as {feed.format}"
            )
        return feed

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

    def stop(self, mode: StopMode = "drain", *, signal: Signals | None = None) -> None:
        """Ask the Run to stop; it returns at once.

        A Drain (`"drain"`) stops claiming new Requests and scheduling those of
        `start()` and `on_idle()`, and finishes the Requests in flight and the queued
        Items within `shutdown.grace` seconds; after that, it turns into a Force Stop.
        The Requests the Callbacks yield meanwhile still enter the Frontier. A Force
        Stop (`"force"`) cancels the Requests in flight and returns them to the
        Frontier without counting an attempt, discards the Items not processed yet,
        counting them as `items.discarded` and returning the Requests they came from
        the same way, and closes the components, each within `shutdown.close_timeout`
        seconds. The Feeds are closed either way, keeping the Items written. A Force
        Stop while the Spider's Accounts are imported, or while the components open,
        cancels that and ends the Run before it starts, closing the components
        already open; one while they close limits the close in progress too.

        It may be called from a Callback, a component, or another task, and before
        the Run starts. A Force Stop requested during a Drain escalates it; anything
        else requested again, and anything requested after the Run, does nothing.

        Args:
            mode: `"drain"` or `"force"`.
            signal: The signal that asked for the stop, which `run()` and
                `pernix run` pass on SIGINT and SIGTERM; the Run's outcome file then
                records the exit code 130 they exit with, unless the Run failed.

        Raises:
            ValueError: `mode` is neither.
        """
        self._shutdown.request(mode)
        if signal is not None:
            self._interrupted = True

    @property
    def paused(self) -> bool:
        """Whether the Run is paused, or will start paused; `False` once it is stopping or over."""
        return self._shutdown.mode is None and self._pausing.paused

    def pause(self) -> None:
        """Pause the Run: stop claiming new Requests until `resume()`; it returns at once.

        The Requests in flight finish, with their Callbacks and Inline Fetches, and are
        settled once their Items are processed, those in a batch without waiting for it
        to fill or time out, so a paused Run holds no Lease once those Items are
        processed. No Request is taken from `start()` or `on_idle()` meanwhile, though
        the one being produced still enters the Frontier, and the Run is not idle, so
        it does not end on its own; a Drain or a Force Stop still ends it. Lease
        renewals, Cooldowns, proxy refreshes, and stats snapshots go on. Pausing is
        logged and emitted as `SpiderPaused`, and the time spent paused is counted in
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
        channel: asyncio.Queue[tuple[Any, Callable[[], None]]] = asyncio.Queue(1)

        async def outlet(item: Any, processed: Callable[[], None]) -> None:
            await channel.put((item, processed))

        self._outlet = outlet
        return ItemStream(self.crawl, self.stop, channel)

    async def crawl(self) -> RunResult:
        """Run the Spider to completion.

        The `pernix.*` log records of the Run are delivered through a `RunLog`, and
        carry its `run_id`. With a Session Pool, the Spider's Accounts are imported
        first, with `Spider.accounts()`. Every component is built and wired, as
        `pernix.run_assembly.RunAssembly.build()` describes, before anything is
        opened. The components are subscribed and opened in order, then the Feeds,
        and `SpiderOpened` is emitted. The Engine
        runs with the Frontier while Items go through the Pipelines into the
        Feeds and the stats are reported; once it is done, the queued Items and the
        pending batches are processed. Then `SpiderClosed` is emitted, the final stats
        snapshot is taken, and the components are closed in reverse order, which
        finishes the Feed files. `stop()` ends the Run early, and `pause()` holds it
        until `resume()`.

        The Frontier is held in memory, or, with the `job_dir` setting, in that
        directory, a `pernix.job_dir.JobDir`, which the Run takes first, before
        anything else: it records a new Run there, or checks that this one continues
        the Run recorded, which it resumes. Once the components are built, and before
        they are opened, the Requests the Frontier holds are checked to name only
        methods the Spider has, and those the last process left leased are made ready
        again, each counting an attempt. A resumed Run logs what was left, sets
        `run.resumed` and `run.first_started_at` in its stats, and iterates
        `Spider.start()` only if no earlier process of the Run iterated it to its end.

        An error that ends the Run is logged, with its traceback unless it is a
        configuration error, and raised. Last, however the Run ends, the file of the
        `outcome_file` setting, if set, is written, as
        `pernix.outcome_file.OutcomeFile` describes; but not in a job directory the
        Run could not take, which it leaves as it was. The job directory is released
        after that.

        Returns:
            How the Run ended.

        Raises:
            RuntimeError: This Crawler already ran.
            ExceptionGroup: Writing an Item to a Feed failed; it holds the error.
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme, or a
                Feed is invalid.
            pernix.errors.JobDirError: Another process holds the job directory, it
                holds another Run, or its Requests name methods the Spider lacks.
        """
        if self._started:
            raise RuntimeError("a Crawler runs only once; create a new one for another Run")
        self._started = True
        if not self._resuming:
            self.started_at = dt.datetime.now(dt.UTC)
        secrets = revealed_secrets(self.settings.values())
        with run_context(self._run_context):
            async with RunLog(self.settings.section(LogSettings), self._run_context, secrets=secrets) as run_log:
                try:
                    try:
                        result = await self._crawl(run_log)
                    except BaseException as exc:
                        _log_failure(exc)
                        reason = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                        self._write_outcome(run_log, reason, exc)
                        raise
                    self._write_outcome(run_log, result.reason)
                    return result
                finally:
                    if self._job is not None:  # in a thread, as it waits for a take() in progress
                        await asyncio.to_thread(self._job.release)

    async def _crawl(self, run_log: RunLog) -> RunResult:
        try:
            async with ComponentLifecycle() as lifecycle:
                self._shutdown.begin(lifecycle)
                if self._job is not None:
                    await self._take_job_dir(self._job)
                accounts: tuple[Account, ...] = ()

                async def import_accounts() -> None:
                    nonlocal accounts
                    accounts = await self._accounts(run_log)

                # The Accounts come before the components are built, as they make up the Session Pool.
                if await self._shutdown.run_stage(import_accounts()):
                    return self._forced_before_start()
                run = RunAssembly.build(
                    self,
                    run_log,
                    output=self._output,
                    outlet=self._outlet,
                    accounts=accounts,
                    pausing=self._pausing,
                    frontier_path=None if self._job is None else self._job.frontier_path,
                    start=None if self._job is None else functools.partial(self._seeding, self._job),
                    control_found=None if self._job is None else self._job.control_found,
                )
                self._shutdown.attach(run.engine)
                try:
                    if self._job is not None and isinstance(run.frontier, SQLiteFrontier):
                        await self._resume(run.frontier, self._job)
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
            self._pausing.end()  # also when the Engine never ran

    async def _take_job_dir(self, job: JobDir) -> None:
        """Take the job directory for the Run, recording it or checking that it continues the Run recorded."""
        record = await asyncio.to_thread(job.take, self._record())
        self._job_taken = True
        self.started_at = record.started_at
        if job.resumed:
            self.stats.set("run.resumed", 1)
            self.stats.set("run.first_started_at", _timestamp(record.started_at))

    def _record(self) -> RunRecord:
        """This Run, as a job directory records it."""
        options = self.settings.section(FingerprintSettings)
        function: object = options.function
        return RunRecord(
            run_id=self.run_id,
            spider=self.spider.name,
            started_at=self.started_at,
            arguments=None if self._arguments is None else _as_json(self._arguments),
            fingerprint={
                "headers": list(options.headers),
                "sort_query": options.sort_query,
                "function": function if function is None or isinstance(function, str) else _import_path(function),
            },
            pernix=version("pernix"),
        )

    async def _resume(self, frontier: SQLiteFrontier, job: JobDir) -> None:
        """Check the Requests of a job directory's Frontier, and make those the last process left leased ready again."""
        try:
            missing = await frontier.missing_methods()
        except SerializationError as exc:  # the database is not one this Pernix reads
            raise JobDirError(str(exc)) from exc
        if missing:
            raise JobDirError(
                f"the job directory {job.path} holds Requests that name methods the Spider "
                f"{self.spider.name!r} does not have: {', '.join(missing)}; were they renamed or removed?"
            )
        released = await frontier.release_leases()
        if job.resumed:
            counts = await frontier.counts()
            logger.info(
                "Resuming the Run first started at %s, with %d Requests ready, %d of them left in flight by the last "
                "process, %d delayed, %d done, and %d dead",
                _timestamp(self.started_at),
                counts.ready,
                released,
                counts.delayed,
                counts.done,
                counts.dead,
            )

    async def _seeding(self, job: JobDir) -> AsyncGenerator[Request]:
        """Yield the Requests of `Spider.start()`, unless an earlier process of the Run yielded them all; record when they are."""
        if job.record is not None and job.record.start_exhausted:
            return
        async with aclosing(iterate_and_close(self.spider.start())) as requests:
            async for request in requests:
                yield request
        # Here, as a Feed records its file, not in a thread, which a Drain cancelling the seeding would leave writing.
        job.record_start_exhausted()

    def _write_outcome(self, run_log: RunLog, reason: str, error: BaseException | None = None) -> None:
        """Write the outcome file, if the `outcome_file` setting names one that no other output writes.

        A job directory the Run could not take is left as it was, without one.
        """
        try:
            path = outcome_path(self, self._output)
        except SettingsError as exc:
            logger.error("The outcome file is not written: %s", "; ".join(exc.problems))
            return
        if path is not None and self._job is not None and not self._job_taken and _within(path, self._job.path):
            logger.error("The outcome file %s is not written: it is in the job directory the Run could not take", path)
            return
        if path is not None:
            outcome = OutcomeFile(self, path)
            outcome.write(reason, error=error, interrupted=self._interrupted, redactor=run_log.redactor)

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


class _RecordedFeed(Feed):
    """A Feed of a Run with a job directory that writes a new file in place, which it records there once begun.

    Recorded only then, a file is continued by a resumed Run only once a process of
    the Run replaced it: one that ends before opening its Feeds, as on a
    configuration error, leaves the files it named as they were, for the Run to
    replace when it resumes.
    """

    def __init__(self, path: Path, options: FeedOptions | None, *, job: JobDir) -> None:
        super().__init__(path, options, writing="in_place")
        self._job = job

    def open(self) -> None:
        """Replace the file, begin the format, and record the file in the job directory.

        Raises:
            pernix.errors.FeedError: The job directory cannot record the file.
        """
        super().open()
        try:
            self._job.record_feed(self.path, self.format)
        except OSError as exc:
            self.close()
            raise FeedError(f"cannot record the feed {self.path} in the job directory: {exc}") from exc


def _log_failure(error: BaseException) -> None:
    """Log an error that ended the Run, with its traceback unless it is a configuration error."""
    if isinstance(error, CONFIGURATION_ERRORS):
        logger.error("%s", error)
    elif isinstance(error, Exception):
        logger.error("The Run failed", exc_info=error)


def _as_request(request: Request | str) -> Request:
    return Request(request) if isinstance(request, str) else request


def _as_json(value: object) -> Any:
    """A value as JSON gives it back, as a job directory records it: tuples as lists, and keys as strings."""
    return msgspec.json.decode(msgspec.json.encode(value, enc_hook=repr))


def _import_path(component: object) -> str:
    """The import path of a component given as an object: of a class or function, or of the class of another object."""
    named = component if hasattr(component, "__qualname__") else type(component)
    return f"{named.__module__}.{getattr(named, '__qualname__', '')}"


def _timestamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds")


def _within(path: os.PathLike[str], directory: os.PathLike[str]) -> bool:
    """Whether a file is in a directory, at any depth."""
    try:
        return Path(path).resolve().is_relative_to(Path(directory).resolve())
    except (OSError, ValueError):
        return False
