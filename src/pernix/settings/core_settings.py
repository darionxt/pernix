from __future__ import annotations

import codecs
import logging
import re
import string
from pathlib import PurePath
from typing import TYPE_CHECKING, Annotated, Any, Literal

import msgspec
from msgspec import structs

from pernix.account import ACCOUNT_FILE_SUFFIXES, Account
from pernix.proxies.json_answers import JsonAnswer  # noqa: TC001  # msgspec resolves the field types at runtime
from pernix.proxies.proxy import Proxy  # noqa: TC001  # msgspec resolves the field types at runtime
from pernix.proxies.proxy_url import proxy_url
from pernix.secret import Secret  # noqa: TC001  # msgspec resolves the field types at runtime
from pernix.settings.section import ComponentMap, NamedComponents, Section
from pernix.text_decoding import codec_for
from pernix.urls import url_host

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "CORE_SECTIONS",
    "DEFAULT_LOCALE",
    "FEED_FORMATS",
    "PATH_FIELDS",
    "BlockedSettings",
    "CoreSettings",
    "DomainSettings",
    "EventSettings",
    "FeedOptions",
    "FeedSettings",
    "FingerprintSettings",
    "FrontierSettings",
    "ItemSettings",
    "LogSettings",
    "NetworkSettings",
    "ProxySettings",
    "ResponseSettings",
    "RetrySettings",
    "SessionSettings",
    "ShutdownSettings",
    "StatsSettings",
    "locale_languages",
]

DEFAULT_LOCALE = ("en-US", "en")
"""The languages presented by default, as Chrome presents them for American English."""

_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*")


class CoreSettings(Section):
    """Settings of the crawl loop itself.

    Attributes:
        concurrency: The maximum number of Requests processed at once that do not
            go to the browser Fetcher; those that do have `browser.max_pages`
            workers of their own, so neither kind waits for the other.
        extensions: Extensions to run, as `{extension: order}`; the built-in
            `OffsiteFilter` is enabled by default.
        addons: Addons to enable, in order, by entry point name, import path, class,
            or object.
        middlewares: Download Middleware, as `{middleware: order}`; lower orders are
            further out.
        spider_middlewares: Spider Middleware, as `{middleware: order}`; lower orders
            are further out.
        fetchers: Fetchers by name, as `{name: fetcher}`; `Request(fetcher=name)`
            selects one by that name. The HTTP Fetcher is `http`, and the browser
            Fetcher, which renders Requests with `render` set, is `browser`.
        pipelines: Item Pipelines, as `{pipeline: order}`; lower orders run first.
        failed_pipelines: Pipelines that receive each `FailedItem`, the Items a
            Pipeline failed on, as `{pipeline: order}`. The built-in
            `FailedItemsFile`, enabled by default, writes them to `items.failed_output`.
        run_id: Identifies the Run in logs, Events, and stats; `None` generates one.
            A Platform passes its own, for example as `PERNIX_RUN_ID`.
        locale: The languages the client presents, in order of preference, as a
            list or a comma-separated string, such as `"de-DE,de,en-US,en"`: the
            browser's `navigator.languages`, from which Chrome makes its
            `Accept-Language`, which the HTTP Fetcher sends too with the pinned
            Profile. Chrome adds languages to some lists, such as English to
            `"de-DE,de"`; the browser Fetcher then warns, naming the list to set.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
    extensions: ComponentMap = msgspec.field(default_factory=lambda: {"pernix.extensions.OffsiteFilter": 100})
    addons: tuple[Any, ...] = ()
    middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    spider_middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    fetchers: NamedComponents = msgspec.field(
        default_factory=lambda: {"http": "pernix.fetchers.HTTPFetcher", "browser": "pernix.fetchers.BrowserFetcher"}
    )
    pipelines: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    failed_pipelines: ComponentMap = msgspec.field(default_factory=lambda: {"pernix.items.FailedItemsFile": 100})
    run_id: Annotated[str, msgspec.Meta(min_length=1)] | None = None
    locale: str | tuple[str, ...] = DEFAULT_LOCALE

    def __post_init__(self) -> None:
        try:
            structs.force_setattr(self, "locale", locale_languages(self.locale))
        except ValueError as exc:
            raise ValueError(f"{self.key('locale')}: {exc}") from None


def locale_languages(locale: str | Sequence[str]) -> tuple[str, ...]:
    """Read the languages a `locale` names.

    Args:
        locale: The languages, in order of preference, as a list or a
            comma-separated string, such as `"en-US,en"`.

    Returns:
        The languages, in order, blank entries left out.

    Raises:
        ValueError: It names none, or something other than a language tag.
    """
    tags = [tag.strip() for tag in (locale.split(",") if isinstance(locale, str) else locale)]
    languages = tuple(tag for tag in tags if tag)
    if not languages:
        raise ValueError(f"name at least one language, such as {','.join(DEFAULT_LOCALE)!r}")
    for tag in languages:
        if _LANGUAGE_TAG.fullmatch(tag) is None:
            raise ValueError(f"{tag!r} is not a language tag, such as 'en-US' or 'de'")
    return languages


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


class SessionSettings(Section):
    """Settings of the Session Pool that Requests lease Sessions from.

    It is off by default, as Requests are independent (ADR 0011). Once enabled,
    every Request leases a Session, rendered or not. With
    Accounts, the pool holds one Session per Account, and no other.

    Attributes:
        enabled: Whether Requests lease Sessions.
        pool: The pool's name.
        size: Sessions the pool holds at most, without Accounts. They are created
            as Requests need them, or under the id a Request names.
        eager: Create all `size` Sessions when the Run starts, without Accounts.
        max_concurrency: Fetches in progress at once per Session, but for those
            bound to an Account, which have their own.
        cooldown: Seconds a 429 without `Retry-After`, or a failed login, cools
            its Session down.
        cooldown_max: The longest a 429 cools its Session down.
        lease_ttl: Seconds a Session stays leased without renewal; leases are
            renewed every third of it while their fetch runs.
        accounts: Accounts to bind Sessions to, as tables with the fields of an
            `pernix.Account`.
        accounts_file: A JSON Lines or CSV file of more Accounts, read when the
            Run starts; see `pernix.account.read_accounts()`.
        login_attempts: Failed logins in a row after which a Session is Retired.
        max_blocks: Blocked Responses in a row after which a Session is Retired; a
            fetch that went well ends the row.
    """

    __prefix__ = "sessions"

    enabled: bool = False
    pool: str = "default"
    size: Annotated[int, msgspec.Meta(ge=1)] = 8
    eager: bool = False
    max_concurrency: Annotated[int, msgspec.Meta(ge=1)] = 1
    cooldown: Annotated[float, msgspec.Meta(ge=0)] = 30.0
    cooldown_max: Annotated[float, msgspec.Meta(ge=0)] = 300.0
    lease_ttl: Annotated[float, msgspec.Meta(gt=0)] = 300.0
    accounts: tuple[Account, ...] = ()
    accounts_file: PurePath | None = None
    login_attempts: Annotated[int, msgspec.Meta(ge=1)] = 3
    max_blocks: Annotated[int, msgspec.Meta(ge=1)] = 3

    def __post_init__(self) -> None:
        if (self.accounts or self.accounts_file is not None) and not self.enabled:
            raise ValueError(
                f"{self.key('accounts')}: Accounts are bound to the Sessions of a Session Pool; "
                f"enable one with {self.key('enabled')}"
            )
        if self.accounts_file is not None and self.accounts_file.suffix.lower() not in ACCOUNT_FILE_SUFFIXES:
            raise ValueError(
                f"{self.key('accounts_file')}: Accounts are read from "
                f"{', '.join(sorted(ACCOUNT_FILE_SUFFIXES))} files, not {self.accounts_file.name!r}"
            )


class ProxySettings(Section):
    """Settings of the proxies Requests are sent through.

    Proxies are used once a source or a `binding` is set; the proxies of every
    source are used together. A Session is bound to one, as `binding` picks it: by
    default, it keeps the one it is bound to while that is available; a `binding`
    may also give a Session a proxy of its own, so that one without a source gives
    the Sessions of a Session Pool their proxies. A Request without a Session goes
    through the next one, in turn; `Request(proxy=...)` goes through its own
    instead. A proxy that fails cools down, longer with each failure in a row, and
    is Retired after `max_failures` in a row. A proxy is a URL such as
    `http://user:password@proxy:8080`, or `proxy:8080` for HTTP, with its port; its
    key is all of it, so that sticky sessions on one gateway, which differ only in
    their credentials, stay apart.

    Attributes:
        urls: Proxies to use, each a URL, or a `pernix.proxies.Proxy` table to tag
            it, such as `{ url = "http://proxy:8080", tags = ["us"] }`.
        file: A text file of more proxies, one per line, read when the Run starts
            and at each refresh; blank lines and those starting with `#` are
            skipped. See `pernix.proxies.read_proxies()`.
        api: The URL of an API answering with more proxies, one per line, fetched
            when the Run starts and at each refresh, as a proxy vendor's does.
            `{count}` in it is replaced by how many proxies are wanted.
        provider: A `pernix.proxies.ProxyProvider` giving more proxies, as an
            object, a class building one, or an import path.
        refresh: Seconds between loads of the file, the API, and the provider,
            whose proxies then replace those they gave before, or are added to them
            as `supply` says; `None` to load them once, when the Run starts.
        supply: How each load lists the proxies the sources give: `replace` those
            listed before, or `add` to them, keeping each until it expires, as for
            an API that answers with new short-lived proxies each time, so that
            Sessions keep theirs.
        api_lifetime: Seconds each proxy of `api` works for, unless its answer tells
            when it expires, as an answer of one per line does not; `None` for no
            expiry. Providers set their records' `expires`.
        api_json: How to read the answers of `api` in JSON, a
            `pernix.proxies.JsonAnswer` table: where the list of proxies is, how
            each entry is written, and when it expires; `None` to read one proxy
            per line.
        min_available: With `supply = "add"`, extract more proxies from the API
            and the provider while fewer than this many are available, and when a
            fetch finds none; `None` to extract only at the loads.
        extract_interval: Seconds at least between two extractions on demand.
        max_extracted: Proxies the API and the provider give at most in a Run, as
            extracting them costs; `None` for no limit.
        extract_attempts: Extractions in a row that give no proxy, after which no
            more are made on demand, so that a Run whose proxies can no longer be
            used drains.
        cooldown: Seconds a proxy cools down after it failed, doubled with each
            failure in a row.
        cooldown_max: The longest a proxy cools down.
        max_failures: Failures in a row after which a proxy is Retired.
        max_sessions: Sessions a proxy is bound to at most, so that with 1 no two
            share one; those that find none wait. `None` for no limit.
        rebind: When the default `binding` binds a Session to another proxy:
            `unavailable` once its proxy cools down, is Retired, or is no longer
            listed; `every_lease` at each Lease, so its Leases go through the
            proxies in turn; `retired` only once its proxy is Retired or no longer
            listed, the Session cooling down with its proxy meanwhile; `never`,
            the Session cooling down with its proxy, and Retired with it.
        binding: A replacement binding policy, a `pernix.proxies.BindingPolicy`
            with `async bind(binding)`, as an object, a class building one, or an
            import path; `None` for the default policy.
    """

    __prefix__ = "proxies"

    urls: tuple[str | Proxy, ...] = ()
    file: PurePath | None = None
    api: Secret | None = None
    provider: Any = None
    refresh: Annotated[float, msgspec.Meta(gt=0)] | None = None
    cooldown: Annotated[float, msgspec.Meta(ge=0)] = 60.0
    cooldown_max: Annotated[float, msgspec.Meta(ge=0)] = 600.0
    max_failures: Annotated[int, msgspec.Meta(ge=1)] = 5
    max_sessions: Annotated[int, msgspec.Meta(ge=1)] | None = None
    rebind: Literal["unavailable", "every_lease", "retired", "never"] = "unavailable"
    binding: Any = None
    supply: Literal["replace", "add"] = "replace"
    api_lifetime: Annotated[float, msgspec.Meta(gt=0)] | None = None
    api_json: JsonAnswer | None = None
    min_available: Annotated[int, msgspec.Meta(ge=1)] | None = None
    extract_interval: Annotated[float, msgspec.Meta(ge=0)] = 1.0
    max_extracted: Annotated[int, msgspec.Meta(ge=1)] | None = None
    extract_attempts: Annotated[int, msgspec.Meta(ge=1)] = 3

    def __post_init__(self) -> None:
        problems: list[str] = []
        for index, url in enumerate(self.urls):
            try:
                proxy_url(url if isinstance(url, str) else url.url)
            except ValueError as exc:
                problems.append(f"{self.key('urls')}[{index}]: {exc}")
        if self.supply != "add":
            add = f'{self.key("supply")} = "add"'
            extracting = [field for field in ("min_available", "max_extracted") if getattr(self, field) is not None]
            problems.extend(f"{self.key(field)}: proxies are extracted with {add} alone" for field in extracting)
        if self.min_available is not None and self.api is None and self.provider is None:
            problems.append(
                f"{self.key('min_available')}: proxies are extracted from {self.key('api')} "
                f"or {self.key('provider')}, and neither is set"
            )
        if self.api_json is not None and self.api is None:
            problems.append(f"{self.key('api_json')}: it tells how to read the answers of {self.key('api')}, unset")
        if self.api_lifetime is not None and self.api is None:
            problems.append(
                f"{self.key('api_lifetime')}: it is the lifetime of the proxies of {self.key('api')}, unset"
            )
        if problems:
            raise ValueError("; ".join(problems))

    @property
    def enabled(self) -> bool:
        """Whether proxies are used: a source of proxies, or a binding policy, is set."""
        return self.sourced or self.binding is not None

    @property
    def sourced(self) -> bool:
        """Whether a source of proxies is set."""
        return bool(self.urls) or self.file is not None or self.api is not None or self.provider is not None


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


class NetworkSettings(Section):
    """Settings of the hosts Requests may reach.

    Attributes:
        public_only: Refuse hosts whose addresses are not public: private, loopback,
            link-local, and others that are not publicly routable, as fetching
            untrusted links calls for. The HTTP Fetcher resolves the host of every
            redirect hop, the first included, before it is sent, and connects only
            to the addresses it checked; through a proxy, which resolves hosts
            itself, only hosts written as addresses, and loopback names, are
            checked. The browser Fetcher refuses to render while it is on, as the
            browser resolves hosts itself. A refused hop raises `NetworkBlocked`.
        allow_hosts: Host names reached whatever their addresses, such as an
            internal service, each matched exactly.
    """

    __prefix__ = "network"

    public_only: bool = False
    allow_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        hosts: list[str] = []
        for host in self.allow_hosts:
            name = _host_name(host)
            if name is None:
                raise ValueError(
                    f"{self.key('allow_hosts')}: {host!r} is not a host name; write one alone, such as "
                    "'wiki.example', without a scheme or a port"
                )
            hosts.append(name)
        structs.force_setattr(self, "allow_hosts", tuple(hosts))


def _host_name(host: str) -> str | None:
    """A host name as `pernix.urls.url_host()` reads it from a URL; `None` for anything but a host alone."""
    host = host.strip()
    bracketed = host.startswith("[") and host.endswith("]")
    if not host or any(char in host for char in "/?#@\\") or (":" in host and not bracketed):
        return None
    return url_host(f"//{host}")


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
        browser_lost: Retries after the browser of a rendered fetch went away
            (`BrowserLost`), at once, apart from `times`; 0 turns them off.
        blocked: Retries after a Response was found Blocked (`Blocked`), at once,
            with another Session, through another proxy, or rendered, apart from
            `times`; 0 turns them off.
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
    browser_lost: Annotated[int, msgspec.Meta(ge=0)] = 3
    blocked: Annotated[int, msgspec.Meta(ge=0)] = 3
    policy: Any = None


class BlockedSettings(Section):
    """Settings of how Responses in which a site refuses their sender are told and handled.

    A Response the Spider's `is_blocked()` finds Blocked is retried at once, under
    the retry budget `blocked` (`retry.blocked`): with another Session, with a
    Session Pool, which counts it toward the Session's Retirement; through another
    proxy, without one; and rendered, with `render`. Without a Session Pool, a
    Response is judged only when its retry can go through another proxy, or be
    rendered; otherwise it reaches its Callback, as any other.

    Attributes:
        statuses: The status codes the default `Spider.is_blocked()` finds Blocked.
            429, Rate Limited, cools a Session down instead, and 401 is for
            `Spider.is_logged_out()`.
        render: Retry an HTTP Request found Blocked rendered, as
            `request.replace(render=True)`, unless it names its Fetcher.
    """

    __prefix__ = "blocked"

    statuses: frozenset[int] = frozenset({403})
    render: bool = False


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


class ShutdownSettings(Section):
    """Settings of stopping a Run.

    Attributes:
        grace: Seconds a Drain may take to finish the Requests in flight and the
            queued Items before it turns into a Force Stop.
        close_timeout: Seconds each component may take to close after a Force Stop
            or a cancellation; 0 sets no limit. A sync `close()` cannot be cancelled,
            so it is never limited. Closing after a Run that finished or drained is
            never limited.
    """

    __prefix__ = "shutdown"

    grace: Annotated[float, msgspec.Meta(ge=0)] = 20.0
    close_timeout: Annotated[float, msgspec.Meta(ge=0)] = 5.0


CORE_SECTIONS: tuple[type[Section], ...] = (
    CoreSettings,
    EventSettings,
    ResponseSettings,
    NetworkSettings,
    FrontierSettings,
    FingerprintSettings,
    RetrySettings,
    BlockedSettings,
    DomainSettings,
    SessionSettings,
    ProxySettings,
    ItemSettings,
    FeedSettings,
    LogSettings,
    StatsSettings,
    ShutdownSettings,
)
"""The Sections every Crawler registers."""
