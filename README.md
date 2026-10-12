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

Each Callback is an async generator that yields Requests to follow and Items (dicts) to store. A Request constructed in a Callback resolves a relative URL against the Response being handled, after redirects and honoring `<base href>`. `run()` blocks until every Request has been fetched; `await Titles.crawl(...)` does the same inside an existing event loop.

## Configuration

Settings come from several layers, from lowest to highest precedence: built-in defaults, Addons, the project file, `PERNIX_*` environment variables, `Spider.settings`, and the `settings=` argument of `run()` / `crawl()`.

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
| `extensions` | `{}` | Extensions to run, as `{extension: order}` |
| `addons` | `()` | Addons to enable, by entry point name or import path |
| `middlewares` | `{}` | Download Middleware, as `{middleware: order}`; lower orders are further out |
| `spider_middlewares` | `{}` | Spider Middleware, as `{middleware: order}`; lower orders are further out |
| `fetchers` | `{"http": HTTPFetcher}` | Fetchers by name; `Request(fetcher=name)` selects one |
| `events.timeout` | `30.0` | Seconds before an async event subscriber is cancelled |
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

## Documentation

- [`CONTEXT.md`](CONTEXT.md): terminology
- [`docs/design.md`](docs/design.md): features and interfaces
- [`docs/adr/`](docs/adr/): architectural decisions
- [`CHANGELOG.md`](CHANGELOG.md): notable changes

## Project layout

```
src/pernix/
├── __init__.py          public API: Spider, Request, Response, Middleware, Extension, ...
├── spider.py            Spider base class and run()/crawl() entry points
├── crawler.py           assembles the components of one Run, builds components
├── engine.py            worker loop: claim, fetch, run Callbacks, settle Leases
├── callbacks.py         runs Callbacks and errbacks, collects their outputs
├── retry_policy.py      retry decisions, backoff, Retry-After
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
├── extension.py         Extension base class
├── addons.py            Addon loading, entry point discovery, setup API
├── feeds.py             Item output files
├── events/
│   ├── event.py         Event base class, Dispatch, Veto
│   ├── event_bus.py     EventBus, events.on()
│   └── lifecycle_events.py core Events of a Run
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
├── settings/
│   ├── loader.py        merges and validates the settings layers
│   ├── sources.py       project file and environment variables
│   ├── settings.py      Settings, the immutable result
│   ├── section.py       Section base class, ComponentMap, NamedComponents
│   ├── core_settings.py the Sections every Crawler registers
│   └── secret.py        Secret, a redacted value
└── testing/
    └── frontier.py      conformance suite for Frontier backends
tests/                   one test module per source module, plus end-to-end crawls
```

Pluggable component families with several implementations, such as Fetchers, Frontiers, and Middleware, each have a subpackage holding their protocol and the built-in implementations; the core model stays at the top level.

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
