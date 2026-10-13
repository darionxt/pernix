from __future__ import annotations

import codecs
import logging
import string
from pathlib import PurePath
from typing import Annotated, Any, Literal

import msgspec

from pernix.settings.section import ComponentMap, NamedComponents, Section
from pernix.text_decoding import codec_for

__all__ = [
    "CORE_SECTIONS",
    "FEED_FORMATS",
    "PATH_FIELDS",
    "CoreSettings",
    "DomainSettings",
    "EventSettings",
    "FeedOptions",
    "FeedSettings",
    "FingerprintSettings",
    "FrontierSettings",
    "ItemSettings",
    "LogSettings",
    "ResponseSettings",
    "RetrySettings",
    "StatsSettings",
]


class CoreSettings(Section):
    """Settings of the crawl loop itself.

    Attributes:
        concurrency: The maximum number of Requests processed at once.
        extensions: Extensions to run, as `{extension: order}`; the built-in
            `OffsiteFilter` is enabled by default.
        addons: Addons to enable, in order, by entry point name, import path, class,
            or object.
        middlewares: Download Middleware, as `{middleware: order}`; lower orders are
            further out.
        spider_middlewares: Spider Middleware, as `{middleware: order}`; lower orders
            are further out.
        fetchers: Fetchers by name, as `{name: fetcher}`; `Request(fetcher=name)`
            selects one by that name.
        pipelines: Item Pipelines, as `{pipeline: order}`; lower orders run first.
        failed_pipelines: Pipelines that receive each `FailedItem`, the Items a
            Pipeline failed on, as `{pipeline: order}`. The built-in
            `FailedItemsFile`, enabled by default, writes them to `items.failed_output`.
        run_id: Identifies the Run in logs, Events, and stats; `None` generates one.
            A Platform passes its own, for example as `PERNIX_RUN_ID`.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
    extensions: ComponentMap = msgspec.field(default_factory=lambda: {"pernix.extensions.OffsiteFilter": 100})
    addons: tuple[Any, ...] = ()
    middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    spider_middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    fetchers: NamedComponents = msgspec.field(default_factory=lambda: {"http": "pernix.fetchers.HTTPFetcher"})
    pipelines: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    failed_pipelines: ComponentMap = msgspec.field(default_factory=lambda: {"pernix.items.FailedItemsFile": 100})
    run_id: Annotated[str, msgspec.Meta(min_length=1)] | None = None


class EventSettings(Section):
    """Settings of the event bus.

    Attributes:
        timeout: Seconds an asynchronous subscriber may run before it is cancelled,
            unless it was subscribed with its own timeout.
    """

    __prefix__ = "events"

    timeout: Annotated[float, msgspec.Meta(gt=0)] = 30.0


class DomainSettings(Section):
    """Settings of how each domain is fetched: its own concurrency, spacing, and rate.

    A domain is a host name; each has its own slot.

    Attributes:
        concurrency: Fetches in progress at once per domain.
        delay: Seconds between the starts of two fetches from one domain.
        delay_jitter: Randomizes each delay between `delay * (1 - delay_jitter)` and
            `delay * (1 + delay_jitter)`; 0 keeps it exact.
        rate: Fetches per second per domain, enforced with GCRA; `None` for no limit.
        burst: Fetches a domain may start at once before `rate` applies.
        cooldown_max: The longest a 429 or 503 with `Retry-After` pauses its domain.
    """

    __prefix__ = "domain"

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 8
    delay: Annotated[float, msgspec.Meta(ge=0)] = 0.0
    delay_jitter: Annotated[float, msgspec.Meta(ge=0, le=1)] = 0.5
    rate: Annotated[float, msgspec.Meta(gt=0)] | None = None
    burst: Annotated[int, msgspec.Meta(ge=1)] = 1
    cooldown_max: Annotated[float, msgspec.Meta(ge=0)] = 300.0


class ResponseSettings(Section):
    """Settings of how Responses are read.

    Attributes:
        default_encoding: The encoding of a body that declares none, is not valid
            UTF-8, and whose encoding cannot be detected. An encoding label or a
            Python codec name.
    """

    __prefix__ = "response"

    default_encoding: str = "utf-8"

    def __post_init__(self) -> None:
        try:
            codec_for(self.default_encoding)
        except LookupError:
            key = self.key("default_encoding")
            raise ValueError(f"{key}: unknown encoding {self.default_encoding!r}") from None


class FrontierSettings(Section):
    """Settings of the Frontier.

    Attributes:
        lease_ttl: Seconds a claimed Request stays leased without renewal; Workers
            renew their Leases every third of it while they work.
    """

    __prefix__ = "frontier"

    lease_ttl: Annotated[float, msgspec.Meta(gt=0)] = 60.0


class FingerprintSettings(Section):
    """Settings of the Fingerprints that decide which Requests are duplicates.

    Attributes:
        headers: Names of request headers whose values take part; never `Cookie`.
        sort_query: Sort query parameters by name, so their order does not matter.
        function: A replacement Fingerprint function, `(request) -> str`, as a
            callable, a class building one, or an import path; `None` for the
            default with the options above.
    """

    __prefix__ = "fingerprint"

    headers: tuple[str, ...] = ()
    sort_query: bool = True
    function: Any = None

    def __post_init__(self) -> None:
        if any(name.strip().lower() == "cookie" for name in self.headers):
            raise ValueError(
                f"{self.key('headers')}: cookies never take part in a Fingerprint (ADR 0011), "
                "so the Cookie header cannot be declared"
            )


class RetrySettings(Section):
    """Settings of retries.

    Attributes:
        times: Retries after the first attempt; 0 turns retrying off.
        statuses: Response status codes that are retried.
        backoff_base: Seconds of the first backoff; each retry doubles it, and the
            delay is chosen at random up to it (full jitter).
        backoff_max: The most seconds a backoff can reach.
        retry_after_max: The most seconds a `Retry-After` header can make a retry wait.
        delay_scale: Multiplies every delay the default policy chooses; 0 retries at
            once, as in tests.
        policy: A replacement retry policy, an object with
            `decide(request, outcome, attempt)`, as an object, a class building one,
            or an import path; `None` for the default policy.
    """

    __prefix__ = "retry"

    times: Annotated[int, msgspec.Meta(ge=0)] = 3
    statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 522, 524})
    backoff_base: Annotated[float, msgspec.Meta(ge=0)] = 1.0
    backoff_max: Annotated[float, msgspec.Meta(ge=0)] = 60.0
    retry_after_max: Annotated[float, msgspec.Meta(ge=0)] = 300.0
    delay_scale: Annotated[float, msgspec.Meta(ge=0)] = 1.0
    policy: Any = None


class ItemSettings(Section):
    """Settings of how Items reach the Pipelines.

    Attributes:
        queue_size: Items waiting for the Pipelines at most; a Callback that
            produces another one waits until there is room.
        concurrency: Items processed at once. With 1, Items reach the Feeds in the
            order they were produced.
        timeout: Seconds one call of a Pipeline, `process()` for an Item or
            `process_batch()` for a batch, may take before it is cancelled and fails
            with `TimeoutError`; 0 for no limit. A Pipeline's own `timeout` overrides
            it. Only async calls can be cancelled.
        failed_output: A JSON-lines file, a path template as for Feeds, where the
            built-in `FailedItemsFile` writes every Item a Pipeline failed on, with
            the Pipeline and the error; `None` keeps no such file.
    """

    __prefix__ = "items"

    queue_size: Annotated[int, msgspec.Meta(ge=1)] = 1000
    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 1
    timeout: Annotated[float, msgspec.Meta(ge=0)] = 60.0
    failed_output: str | None = None

    def __post_init__(self) -> None:
        if self.failed_output is not None:
            _check_path_template(self.key("failed_output"), self.failed_output)


FEED_FORMATS: dict[str, Literal["jsonl", "csv", "json"]] = {
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".jl": "jsonl",
    ".csv": "csv",
    ".json": "json",
}
"""The Feed format each file extension implies."""

PATH_FIELDS = frozenset({"name", "run_id", "time"})
"""The placeholders of Feed path templates."""


class FeedOptions(msgspec.Struct, frozen=True, kw_only=True, forbid_unknown_fields=True):
    """How one Feed writes its file.

    Attributes:
        format: `"jsonl"`, `"csv"`, or `"json"`; `None` infers it from the file
            extension (see `FEED_FORMATS`).
        fields: The fields to write, in order; `None` writes every field. For csv
            they are the columns, which otherwise come from the first Item.
        accepts: Item types, as classes or import paths, or a function
            `(item) -> bool`, that the Feed writes; `None` writes every Item.
        encoding: The text encoding of the file.
    """

    format: Literal["jsonl", "csv", "json"] | None = None
    fields: tuple[str, ...] | None = None
    accepts: Any = None
    encoding: str = "utf-8"


class FeedSettings(Section):
    """Settings of the files Items are written to.

    Attributes:
        feeds: Feeds by path template, as `{path: options}`. A path may contain
            `{name}` (the Spider), `{run_id}`, and `{time}` (when the Run started,
            in UTC, such as `20261013T080405Z`).
    """

    feeds: dict[str, FeedOptions] = msgspec.field(default_factory=dict[str, FeedOptions])

    def __post_init__(self) -> None:
        for path, options in self.feeds.items():
            key = f"{self.key('feeds')}[{path!r}]"
            _check_path_template(key, path)
            if options.format is None and PurePath(path).suffix.lower() not in FEED_FORMATS:
                raise ValueError(
                    f"{key}: cannot infer the format from the extension; set format to jsonl, csv, or json"
                )
            try:
                codecs.lookup(options.encoding)
            except LookupError:
                raise ValueError(f"{key}: unknown encoding {options.encoding!r}") from None


def _check_path_template(key: str, path: str) -> None:
    try:
        fields = {field for _, field, _, _ in string.Formatter().parse(path) if field is not None}
    except ValueError as exc:
        raise ValueError(f"{key}: invalid path template {path!r}: {exc}") from None
    unknown = fields - PATH_FIELDS
    if unknown:
        raise ValueError(f"{key}: unknown placeholders {sorted(unknown)}; use {{name}}, {{run_id}}, or {{time}}")


class LogSettings(Section):
    """Settings of the log output of a Run.

    Attributes:
        output: Where the `pernix.*` records go: `"stderr"` or `"stdout"`, written by
            Pernix; `"root"`, handed to the handlers of the root logger as the
            application configured them; or `"auto"`, which is `"root"` when the root
            logger has handlers and `"stderr"` otherwise.
        format: `"text"` for human-readable lines, `"json"` for JSON lines, or
            `"auto"`, which is text on a terminal and JSON lines otherwise. Applies
            when Pernix writes the output.
        level: The lowest level written, such as `"INFO"` or `"debug"`. Applies when
            Pernix writes the output; the root handlers use the application's levels.
        queue_size: Records waiting to be written at most; further records are
            dropped and counted.
        redact: Names whose values are masked in log output besides the built-in
            ones, such as a query parameter, a header, or a JSON key.
    """

    __prefix__ = "log"

    output: Literal["auto", "stderr", "stdout", "root"] = "auto"
    format: Literal["auto", "text", "json"] = "auto"
    level: str = "INFO"
    queue_size: Annotated[int, msgspec.Meta(ge=1)] = 10_000
    redact: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.level.upper() not in logging.getLevelNamesMapping():
            raise ValueError(
                f"{self.key('level')}: unknown level {self.level!r}, use DEBUG, INFO, WARNING, ERROR, or CRITICAL"
            )


class StatsSettings(Section):
    """Settings of the stats of a Run.

    Attributes:
        interval: Seconds between two stats snapshots; 0 turns periodic snapshots
            off. The final snapshot is always taken.
        sinks: Stats Sinks that receive every snapshot, as `{sink: order}`.
    """

    __prefix__ = "stats"

    interval: Annotated[float, msgspec.Meta(ge=0)] = 60.0
    sinks: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])


CORE_SECTIONS: tuple[type[Section], ...] = (
    CoreSettings,
    EventSettings,
    ResponseSettings,
    FrontierSettings,
    FingerprintSettings,
    RetrySettings,
    DomainSettings,
    ItemSettings,
    FeedSettings,
    LogSettings,
    StatsSettings,
)
"""The Sections every Crawler registers."""
