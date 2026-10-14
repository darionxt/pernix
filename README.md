# Pernix

An asyncio crawling framework for Python. HTTP (curl-cffi) and the browser (patchright) are peer ways of fetching, selectable per request. The kernel is minimal; retries, deduplication, rate limiting, sessions, and output are replaceable components, extended through Middleware and Events.

> Status: pre-alpha. The API is not stable yet, and only the HTTP crawl loop is implemented so far.

## Installation

Pernix is not published yet. Install it from a checkout:

```bash
uv add git+https://github.com/darionxt/pernix
```

Requires Python 3.13 or newer.

## Quick start

```python
from pernix import Request, Response, Spider


class Titles(Spider):
    start_urls = ["https://example.com/"]

    async def parse(self, response: Response):
        yield {"url": response.url, "title": response.css("title::text").get()}
        for href in response.css("a::attr(href)").getall():
            request = Request(href)  # a relative link resolves against this Response
            if request.url.startswith("https://example.com/"):
                yield request


if __name__ == "__main__":
    Titles.run(output="titles.jsonl")
```

Each Callback is an async generator that yields Requests to follow and Items to store: dicts, dataclasses, msgspec Structs, pydantic models, or attrs objects. `output=` writes them to a `.jsonl`, `.csv`, or `.json` file. A Request constructed in a Callback resolves a relative URL against the Response being handled, after redirects and honoring `<base href>`. `run()` blocks until every Request has been fetched.

## Running and stopping

```python
class Search(Spider):
    def __init__(self, keyword: str, max_pages: int = 10):  # Spider arguments
        self.keyword = keyword
        self.max_pages = max_pages

    async def start(self):
        yield Request(f"https://example.com/search?q={self.keyword}")


result = Search.run(keyword="dune", max_pages="3", output="results.jsonl")  # as a program
result = await Search.crawl(keyword="dune")  # inside an existing event loop

async with Search.stream(keyword="dune") as items:  # the Items that pass every Pipeline
    async for item in items:
        ...
```

Keyword arguments other than `settings=` and `output=` are Spider arguments, declared by the parameters of `__init__`. Strings, as from a command line, are converted to the annotated types: `"3"` to `3`, `"true"` to `True`, and JSON to lists, dicts, dataclasses, and Structs; `__init__` may raise `ValueError` to reject a value. `run()` and `crawl()` return a `RunResult` with the `run_id`, the `reason` the Run ended, and the final stats. `stream()` holds one Item at a time, so a slow consumer pauses the Callbacks; leaving the loop early stops the Run.

`crawler.stop()` drains a Run: no new Request is claimed, and the Requests in flight and the queued Items are finished within `shutdown.grace`. `crawler.stop("force")` cancels the Requests in flight and returns them to the Frontier, and discards the Items not processed yet; the Feeds are still completed. `run()` drains on the first SIGINT or SIGTERM and force-stops on the second, and ends the process with an exit code when the Run does not complete:

| Exit code | Meaning |
| --- | --- |
| `0` | The Run finished, or code stopped it |
| `1` | An error ended the Run; it is logged with its traceback |
| `2` | Invalid settings, Spider arguments, or components; nothing was fetched |
| `130` | A signal stopped the Run |

`crawl()` and `stream()` raise errors instead, and leave signals to your application. See [`docs/design.md`](docs/design.md) §4.12 and §6.6.

## Configuration

Settings come from several layers, from lowest to highest precedence: built-in defaults, Addons, the project file, `PERNIX_*` environment variables, `Spider.settings`, and the `settings=` argument of `run()`, `crawl()`, and `stream()`.

```python
class Titles(Spider):
    settings = {"concurrency": 8}


Titles.run(output="titles.jsonl", settings={"concurrency": 4})
```

```toml
# pernix.toml, or [tool.pernix] in pyproject.toml; found by walking up from the working directory
concurrency = 8
```

```bash
PERNIX_CONCURRENCY=4 python titles.py
```

Keys are dotted (`domain.delay` is `PERNIX_DOMAIN__DELAY` in the environment). Settings are validated at startup: a misspelled key fails with a suggestion, and a wrong value names the layer it came from. See [`docs/design.md`](docs/design.md) §4.11.

| Key | Default | Meaning |
| --- | --- | --- |
| `concurrency` | `16` | Maximum number of Requests processed at once |
| `extensions` | `{OffsiteFilter: 100}` | Extensions to run, as `{extension: order}` |
| `addons` | `()` | Addons to enable, by entry point name or import path |
| `middlewares` | `{}` | Download Middleware, as `{middleware: order}`; lower orders are further out |
| `spider_middlewares` | `{}` | Spider Middleware, as `{middleware: order}`; lower orders are further out |
| `fetchers` | `{"http": HTTPFetcher}` | Fetchers by name; `Request(fetcher=name)` selects one |
| `pipelines` | `{}` | Item Pipelines, as `{pipeline: order}`; lower orders run first |
| `failed_pipelines` | `{FailedItemsFile: 100}` | Pipelines that receive the Items a Pipeline failed on, as `FailedItem`s |
| `feeds` | `{}` | Files Items are written to, as `{path_template: options}` |
| `run_id` | `None` | Identifies the Run in logs and stats; `None` generates one |
| `events.timeout` | `30.0` | Seconds before an async event subscriber is cancelled |
| `domain.concurrency` | `8` | Fetches in progress at once per domain (host) |
| `domain.delay` / `domain.delay_jitter` | `0.0` / `0.5` | Seconds between the starts of two fetches from one domain, randomized by ± the jitter |
| `domain.rate` / `domain.burst` | `None` / `1` | Fetches per second per domain (GCRA), and how many may start at once |
| `domain.cooldown_max` | `300.0` | The longest a 429 or 503 with `Retry-After` pauses its domain |
| `response.default_encoding` | `"utf-8"` | Encoding of a body that declares none, is not UTF-8, and defies detection |
| `frontier.lease_ttl` | `60.0` | Seconds a claimed Request stays leased without renewal |
| `fingerprint.headers` | `()` | Request headers whose values take part in Fingerprints; never `Cookie` |
| `fingerprint.sort_query` | `true` | Sort query parameters, so their order does not make a Request new |
| `fingerprint.function` | `None` | A replacement Fingerprint function, `(request) -> str` |
| `retry.times` | `3` | Retries after the first attempt; `0` turns retrying off |
| `retry.statuses` | `[408, 429, 500, 502, 503, 504, 522, 524]` | Response status codes that are retried |
| `retry.backoff_base` / `retry.backoff_max` | `1.0` / `60.0` | Full-jitter backoff: seconds of the first retry, doubling up to the maximum |
| `retry.retry_after_max` | `300.0` | The longest wait a `Retry-After` header can set |
| `retry.delay_scale` | `1.0` | Multiplies every retry delay; `0` retries at once, as in tests |
| `retry.policy` | `None` | A replacement retry policy, an object with `decide(request, outcome, attempt)` |
| `log.output` | `"auto"` | `"stderr"`, `"stdout"`, or `"root"` (the application's handlers); `"auto"` is `"root"` if logging is configured, else `"stderr"` |
| `log.format` | `"auto"` | `"text"`, `"json"` (JSON lines), or `"auto"`: text on a terminal, JSON lines otherwise |
| `log.level` | `"INFO"` | The lowest level Pernix writes |
| `log.queue_size` | `10000` | Records waiting to be written at most; more are dropped and counted |
| `log.redact` | `()` | Names whose values are masked in logs, besides the built-in ones |
| `stats.interval` | `60.0` | Seconds between stats snapshots; `0` keeps only the final one |
| `stats.sinks` | `{}` | Stats Sinks, as `{sink: order}`, that receive every snapshot |
| `items.queue_size` | `1000` | Items waiting for the Pipelines at most; a Callback producing more waits |
| `items.concurrency` | `1` | Items processed at once; with 1, Feeds keep the order Items were produced in |
| `items.timeout` | `60.0` | Seconds one Pipeline call may take before it fails with `TimeoutError`; `0` for no limit |
| `items.failed_output` | `None` | The JSON-lines file where `FailedItemsFile` writes failed Items, with the error |
| `shutdown.grace` | `20.0` | Seconds a Drain may take before it turns into a Force Stop |
| `shutdown.close_timeout` | `5.0` | Seconds each component may take to close after a Force Stop; `0` for no limit |

## Items, Pipelines, and Feeds

```python
from pernix import DropItem, Pipeline


class Clean(Pipeline):
    async def process(self, item, ctx):
        if not item["title"]:
            raise DropItem("empty title")  # returning None drops the Item too
        return item


class SaveRows(Pipeline):
    accepts = (Book,)  # only these Item types; or a function (item) -> bool
    batch_size = 500  # process_batch() receives up to 500 Items at once
    retry_times = 3  # retry on OSError and TimeoutError, with backoff
    timeout = 300  # seconds per call, instead of items.timeout

    async def process_batch(self, items, contexts): ...


class Books(Spider):
    settings = {
        "pipelines": {Clean: 100, SaveRows: 200},
        "feeds": {"out/{name}-{time}.csv": {"fields": ["title", "price"]}},
    }
```

Pipelines run in order on each Item, apart from the Callbacks: when they fall behind, the Item queue fills and Callbacks wait. A Pipeline error or timeout drops the Item (emitted as `ItemError`) and never fetches the Request again; the Item then goes, as a `FailedItem`, through the `failed_pipelines`, such as the built-in `FailedItemsFile` that writes to `items.failed_output`. Feeds write the Items that pass every Pipeline; each file is written under a temporary name and moved into place when the Run ends, so it appears complete or not at all. See [`docs/design.md`](docs/design.md) §4.8.

## Logs and stats

Pernix logs on the `pernix.*` loggers; `self.logger` in a Spider is `pernix.spiders.<name>`. During a Run, records are queued and written by a background thread, so a slow log output never stalls the crawl. If your application configured logging, the records go to its handlers; otherwise Pernix writes them to stderr, as text on a terminal and as JSON lines otherwise. Every record carries the `run_id`, the `spider`, and the `request_id` of the Request being processed, and credentials, cookies, tokens, and `Secret` settings are masked.

The stats of a Run, such as `downloads.by_status.200` or `items.scraped`, are in `crawler.stats` (`self.stats` in a Spider, where you can add your own). They are logged every minute and summarized at the end, and Stats Sinks registered with `stats.sinks` receive every snapshot. See [`docs/design.md`](docs/design.md) §8 for the keys.

## Documentation

- [`CONTEXT.md`](CONTEXT.md): terminology
- [`docs/design.md`](docs/design.md): features and interfaces
- [`docs/adr/`](docs/adr/): architectural decisions
- [`CHANGELOG.md`](CHANGELOG.md): notable changes

## Project layout

```
src/pernix/
├── __init__.py          public API: Spider, Request, Response, Middleware, Extension, ...
├── spider.py            Spider base class and the run()/crawl()/stream() entry points
├── spider_arguments.py  checks and converts Spider arguments
├── crawler.py           Crawler: runs one Run, builds components with injection, stops it
├── run_assembly.py      which components a Run has, built and wired before opening
├── engine.py            worker loop: claim, process, settle Leases; idle detection, Drain
├── request_processor.py one attempt at a Request: fetch, retry decision, Callbacks
├── shutdown.py          Drain, its grace period, and Force Stop
├── runner.py            run() as a program: exit codes
├── stop_signals.py      SIGINT and SIGTERM stop a Run
├── run_result.py        RunResult, how a Run ended
├── callbacks.py         runs Callbacks and errbacks, collects their outputs
├── retry_policy.py      retry decisions, backoff, Retry-After
├── throttle.py          per-domain slots: concurrency, delay, rate, cooldowns
├── request.py           Request
├── request_codec.py     persisting Requests: msgpack, Callbacks by name
├── response.py          Response, selectors, URL joining
├── headers.py           Headers
├── cookies.py           applies Set-Cookie headers to cookies held by name
├── urls.py              URL clean-up, joining, and normalization
├── text_decoding.py     decoding bodies: encoding declarations, labels, detection
├── fingerprint.py       Fingerprints of Requests
├── errors.py            exception hierarchy, control-flow exceptions
├── components.py        loads components from import paths and builds them
├── component_lifecycle.py opens components in order, closes them in reverse
├── addons.py            Addon loading, entry point discovery, setup API
├── items/
│   ├── item_adapter.py  which outputs are Items, and their conversion to dicts
│   ├── pipeline.py      Pipeline base class, ItemContext
│   ├── pipeline_stage.py one Pipeline's filter, options, timeouts, and retries
│   ├── failed_items_file.py FailedItemsFile: writes failed Items to a file
│   ├── item_processor.py the Item queue, Pipelines, batches, and Feed delivery
│   └── item_stream.py   ItemStream, the Items of a streamed Run
├── feeds/
│   ├── feed.py          Feed: one output file, finished atomically; path templates
│   └── feed_formats.py  jsonl, csv, and json layouts
├── logs/
│   ├── log_context.py   run and Request context carried in contextvars
│   ├── queue_handler.py queues the records of a Run without blocking
│   ├── run_log.py       RunLog: the writer thread and the output of a Run
│   ├── log_formats.py   text and JSON-lines formatters
│   └── redaction.py     masks credentials, tokens, and secrets in log text
├── events/
│   ├── event.py         Event base class, Dispatch, Veto
│   ├── event_bus.py     EventBus, events.on()
│   └── lifecycle_events.py core Events of a Run
├── extensions/
│   ├── extension.py     Extension base class
│   └── offsite.py       OffsiteFilter: drops Requests outside allowed_domains
├── fetchers/
│   ├── fetcher.py       Fetcher protocol
│   ├── fetcher_dispatch.py picks the Fetcher of each Request
│   └── http_fetcher.py  curl-cffi HTTP Fetcher
├── frontiers/
│   ├── frontier.py      Frontier protocol, Lease
│   └── memory_frontier.py in-memory Frontier
├── middlewares/
│   ├── middleware.py    download Middleware, NodeMiddleware, the download chain
│   └── spider_middleware.py spider Middleware and their chain
├── stats/
│   ├── stats.py         Stats, the counters and values of a Run
│   ├── core_stats.py    counts Requests, downloads, Responses, and Items
│   ├── stats_reporter.py periodic and final snapshots, the summary
│   ├── loop_lag.py      samples the event loop lag
│   └── stats_sink.py    StatsSink protocol
├── settings/
│   ├── loader.py        merges and validates the settings layers
│   ├── sources.py       project file and environment variables
│   ├── settings.py      Settings, the immutable result
│   ├── section.py       Section base class, ComponentMap, NamedComponents
│   ├── core_settings.py the Sections every Crawler registers
│   ├── conversion.py    validates values, converting strings from the environment
│   └── secret.py        Secret, a redacted value
└── testing/
    └── frontier.py      conformance suite for Frontier backends
tests/                   one test module per source module, plus end-to-end crawls
```

Pluggable component families with several implementations, such as Extensions, Feeds, Fetchers, Frontiers, Middleware, and Pipelines, each have a subpackage holding their protocol and the built-in implementations; the core model stays at the top level.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyright
```

## License

[Apache-2.0](LICENSE)
