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
            if href.startswith("https://example.com/"):
                yield Request(href, callback=self.parse)


if __name__ == "__main__":
    Titles.run(output="titles.jsonl")
```

Each Callback is an async generator that yields Requests to follow and Items (dicts) to store. `run()` blocks until every Request has been fetched; `await Titles.crawl(...)` does the same inside an existing event loop.

## Documentation

- [`CONTEXT.md`](CONTEXT.md): terminology
- [`docs/design.md`](docs/design.md): features and interfaces
- [`docs/adr/`](docs/adr/): architectural decisions
- [`CHANGELOG.md`](CHANGELOG.md): notable changes

## Project layout

```
src/pernix/
├── __init__.py          public API: Spider, Request, Response, Headers
├── spider.py            Spider base class and run()/crawl() entry points
├── request.py           Request
├── response.py          Response, selectors
├── headers.py           Headers
├── crawler.py           assembles the components of one Run
├── engine.py            worker loop: fetch, run Callbacks, dispatch outputs
├── memory_frontier.py   in-memory Frontier
├── feeds.py             Item output files
└── fetchers/
    ├── fetcher.py       Fetcher protocol
    └── http_fetcher.py  curl-cffi HTTP Fetcher
tests/                   one test module per source module, plus end-to-end crawls
```

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
