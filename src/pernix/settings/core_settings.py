from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import msgspec

from pernix.settings.section import ComponentMap, NamedComponents, Section
from pernix.text_decoding import codec_for

__all__ = [
    "CORE_SECTIONS",
    "CoreSettings",
    "DomainSettings",
    "EventSettings",
    "FingerprintSettings",
    "FrontierSettings",
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
        run_id: Identifies the Run in logs, Events, and stats; `None` generates one.
            A Platform passes its own, for example as `PERNIX_RUN_ID`.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
    extensions: ComponentMap = msgspec.field(default_factory=lambda: {"pernix.extensions.OffsiteFilter": 100})
    addons: tuple[Any, ...] = ()
    middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    spider_middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    fetchers: NamedComponents = msgspec.field(default_factory=lambda: {"http": "pernix.fetchers.HTTPFetcher"})
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
    LogSettings,
    StatsSettings,
)
"""The Sections every Crawler registers."""
