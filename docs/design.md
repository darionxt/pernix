# Pernix Design

> Status: confirmed (2026-10-11).
> Terminology follows `CONTEXT.md`; key decisions and their rationale are in `docs/adr/`. This document describes features and interfaces and is updated as implementation proceeds.

## 1. Positioning and boundaries

**What Pernix is**: An asyncio-based Python crawling framework. HTTP fetching uses curl-cffi and browser fetching uses patchright; the two are peer ways of fetching and can be chosen per Request. The kernel is kept as small as possible, and every other capability is a replaceable component that can be disabled. The extension points are Middleware and Events.

**Design principles**

1. **Minimal kernel**: The kernel contains only Request/Response, the scheduling loop, the Middleware chain, the event bus, component lifecycle, and settings. Retries, deduplication, rate limiting, robots, Cookies, stats, and output are built-in components enabled by default; they use the same extension interfaces as user components and can be replaced or turned off.
2. **Works out of the box, without over-wrapping**: Common cases run with zero configuration. When low-level control is needed, curl-cffi's parameters and patchright's `Page` are directly accessible, with no need to bypass the framework.
3. **Explicit semantics**: The behavior of cancellation, timeouts, retries, stopping, and resuming is documented and covered by tests, rather than relying on things that "happen to work".
4. **Safe defaults**: TLS verification is on by default; persistence never uses pickle/eval; proxy credentials, Cookies, and Tokens are automatically redacted in logs and stats.

**Non-goals** (these belong to a future management Platform or to external services)

- Project management, deployment, scheduled triggers, and the scheduling and UI for starting and stopping Runs.
- Authoritative state for cross-project quotas and resource pools (for example, a global proxy pool service).
- CAPTCHA solving and LLM extraction (the latter may become an optional extension in the long term).

The interface between the framework and the Platform is described in §8: the framework is responsible for "correctness within a single Run" and for being "observable and controllable"; the Platform is responsible for "when to run, how many to run, and what to do once they finish".

## 2. Core concepts

| Term | Meaning |
| --- | --- |
| **Spider** | The user's crawling logic: initial Requests, Callbacks, Spider-level settings |
| **Request** | An immutable description of one fetch, including which Callback receives the Response; serializable |
| **Response** | The unified result of one fetch (the same for HTTP and the browser): bytes, lazily decoded text, selectors |
| **Item** | A data object produced by a Callback: dict, dataclass, pydantic, msgspec, and attrs all work |
| **Fetcher** | The transport that executes a Request and produces a Response: `http` and `browser` are built in, and custom ones can be registered (for example `s3`, `ftp`) |
| **Session** | What a Request is sent as: Cookie/storage state, a bound proxy, a Profile, and an optional Account |
| **Profile** | A mutually consistent set of client characteristics (TLS, HTTP/2, request headers, UA version) |
| **Fingerprint** | The key that decides whether two Requests are duplicates |
| **Frontier** | The store of Requests waiting to be fetched; handles ordering, deduplication, Leases, and acknowledgement (ack) |
| **Middleware** | An onion-style layer wrapped around fetching (download Middleware) or around a Callback (spider Middleware) |
| **Pipeline** | The processing chain for Items: validation, cleaning, storage |
| **Event** | A lifecycle notification, for example Spider opened, Response received, Item produced, Spider closed |
| **Crawler** | The composition root of one Run: holds the settings, the event bus, the stats, and all components; has a unique `run_id` |
| **Engine** | The scheduling loop inside a Crawler |

## 3. Architecture overview

```mermaid
flowchart LR
    Start[Spider.start / Callback output] -->|Request| Gate{Admission gate\nOffsite / deduplication / custom gate}
    Gate --> Frontier[(Frontier)]
    Frontier -->|claim with Lease| Worker[Engine worker]
    Worker --> Slot[Concurrency slot / rate limiting]
    Slot --> DMW[Download Middleware onion\nrobots / Session / custom]
    DMW --> Fetcher[Fetcher\nhttp / browser / custom]
    Fetcher --> DMW
    DMW --> SMW[Spider Middleware onion]
    SMW --> Callback[Callback]
    Callback -->|Request| Gate
    Callback -->|Item| ItemQ[Bounded Item queue] --> Pipelines --> Sinks[(Files / databases)]
    Worker -->|ack / nack(delay) / dead| Frontier
```

The lifecycle of a Request:

1. A Spider's `start()` or a Callback produces a Request, which passes through the admission gate (offsite filtering, deduplication, user-defined gates) and enters the Frontier.
2. An Engine worker claims the Request from the Frontier (with a Lease), first acquires the global and per-domain concurrency slots, then passes through rate limiting.
3. The Request passes through the download Middleware onion and reaches the matching Fetcher, which performs the fetch; the Response travels back through the onion along the same path.
4. The Response passes through the spider Middleware onion and is handed to the Callback. Requests produced by the Callback go back to step 1; Items enter a bounded queue and are then handed to the Pipelines.
5. After the Callback finishes, the worker acknowledges (ack) the Request to the Frontier; Requests to be retried later are returned (nack with a delay), and Requests that have failed permanently become Dead Requests.

## 4. User API

### 4.1 Minimal example

```python
from pernix import Spider, Request, Response

class Books(Spider):
    name = "books"
    start_urls = ["https://books.example.com/"]
    settings = {"concurrency": 16, "domain.delay": 0.5}

    async def parse(self, response: Response):
        for href in response.css("article h3 a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)   # relative URLs resolve against the current Response
        if next_url := response.css("li.next a::attr(href)").get():
            yield Request(next_url)

    async def parse_book(self, response: Response):
        yield {
            "title": response.css("h1::text").get(),
            "price": response.css(".price_color::text").get(),
        }

if __name__ == "__main__":
    Books.run(output="books.jsonl")
```

### 4.2 Spider

```python
class Search(Spider):
    name = "search"
    settings = {...}                     # Spider-level settings, see §4.11

    def __init__(self, keyword: str, max_pages: int = 10):
        # Spider arguments: both CLI -a keyword=xx and Platform-supplied arguments are converted and validated by type annotation
        self.keyword = keyword
        self.max_pages = max_pages

    async def start(self):               # optional; falls back to start_urls if omitted
        yield Request(f"https://api.example.com/s?q={self.keyword}", callback=self.parse_list)

    async def on_start(self): ...        # optional: before crawling starts (e.g. to warm up a login)
    async def on_idle(self): ...         # optional: yields more Requests when the Frontier is idle; the Run ends once it adds none
    async def on_close(self, reason): ...  # optional: on close
```

- One Crawler corresponds to one Spider object. Settings live on the Crawler object and no module-level global variable is modified, so one process can run multiple Spiders in parallel.
- **Spider arguments** are the keyword parameters of `__init__`, given by name. A string is converted to the annotated type the way environment variables are (§4.11): `"3"` to `3`, `"true"` to `True`, and JSON to lists, dicts, dataclasses, and Structs; any other value must already have that type, and an unannotated parameter takes it as is. `**kwargs` accepts arguments no parameter names. Missing, unknown (with a suggestion), and invalid arguments are reported together as `SpiderArgumentError`, as is a `ValueError` raised by `__init__`, so a Spider can check its arguments further. `settings` and `output` are reserved for the entry points (§4.12), and a required positional-only parameter is rejected when the class is defined.
- `self.crawler` (set once a Crawler is created for the Spider), `self.stats` (the Run's stats), and `self.logger` (the `pernix.spiders.<name>` logger) are available at runtime.

### 4.3 Request

```python
Request(
    url,
    method=None,           # GET, or POST when a body is given
    params=None,           # query parameters added to the URL
    headers=None,          # order-preserving, case-preserving, duplicate keys allowed
    body=None, json=None, data=None,
    callback=None,         # defaults to parse; must be a Spider method (referenced by name when persisted)
    errback=None,          # a Spider method called as errback(request, error) when the Request fails for good
    kwargs=None,           # keyword arguments passed through to the Callback
    meta=None,             # user data; must be JSON-serializable
    priority=0,            # higher goes first; FIFO within the same priority
    dont_filter=False,     # skip deduplication
    fingerprint=None,      # application-supplied Fingerprint; computed by default, see §6.2
    cookies=None,          # name -> value, sent with this Request only; cannot be combined with a Cookie header
    session=None,          # pin a Session; none by default (Requests are independent), see §5.3
    render=None,           # browser Render options, see §4.7
    fetcher=None,          # explicit Fetcher; inferred from render and the URL scheme by default
    timeout=None,
    proxy=None, impersonate=None,   # per-Request overrides, rarely needed
    ext=None,              # component-private data, by component name
)
```

- Requests are immutable; derive new ones with `request.replace(...)`.
- Without `method`, a Request with a body (`body`, `json`, or `data`) is a POST, and one without is a GET. An explicit method always wins, including a GET with a body, and `replace()` keeps the method as it is.
- `params` adds query parameters to the URL, after those it has and before the fragment: strings and numbers, with a sequence of values repeating a name. Booleans are refused, as sites spell them differently. Like `json` and `data`, it exists only when constructing a Request.
- Requests are independent by default (ADR 0011): no cookies persist from one Request to another. A Request sends only the cookies it carries (`cookies=`, which cannot be combined with a `Cookie` header, and refuses a name that is empty or has `=`, `;`, or a control character, and a value with `;` or a control character). Within one Request's redirect chain cookies flow as in a browser. To continue a conversation explicitly, pass `cookies=response.cookies`; to keep state across many Requests, use a Session Pool (§5.3).
- Relative URLs: a Request created while a Callback is running resolves a relative `url` against that Callback's Response, using its final URL after redirects and honoring `<base href>` in HTML. `request.url` is therefore absolute as soon as the Request is constructed; this also applies to Inline Fetch. Outside a Callback (for example in `start()` or `on_idle()`, or in an Event subscriber), `url` must be absolute, and a relative URL raises `ValueError`. As in browsers, leading and trailing whitespace, tabs, and newlines are removed from every URL.
- `meta` holds only user data. Framework-internal state (retry count, the Session used, etc.) lives in typed fields, and component-private data lives in namespaces keyed by component name, so different components do not become implicitly coupled through `meta`.
- Serialization uses msgspec (msgpack) with a schema version. Callbacks are referenced by method name; if the method cannot be found on resume, an error is raised immediately, and it never silently falls back to `parse`. `kwargs`, `meta`, and `ext` must be JSON-compatible, and a violation names the offending key. The schema version changes only for incompatible changes: added fields have defaults, and unknown fields are ignored.

### 4.4 Response

- Common fields: `url` (final URL), `status`, `headers` (multi-valued), `body: bytes`, `request`, `meta`, `session` (the id of the Session the Request was sent under, if any), `cookies`, `history` (the redirects followed, oldest first, as `Redirect(url, status, headers)` records), `elapsed` (seconds), `ip_address`, `http_version` (`"HTTP/1.1"`, `"HTTP/2"`, `"HTTP/3"`), `fetcher` (such as `"http"`), `streamed` (the body is read as it arrives, apart from the Response, whose `body` is then empty; see §4.6).
- Responses are immutable; `response.replace(...)` derives a copy, for example `response.replace(encoding="gbk")` to decode the same body with another encoding.
- `cookies`: the cookies the client holds after the fetch, as name → value: those the Request carried (`cookies=` or a `Cookie` header) plus those set during the fetch, including on redirects, with expired ones removed. `Set-Cookie` is parsed as one cookie per header, ignoring attributes other than `Max-Age` and `Expires`. Domain and path are not tracked, so same-named cookies collapse into one; full cookie records are kept in a Session's cookie jar (§5.3), whose other cookies `Response.cookies` leaves out. `Request(next_url, cookies=response.cookies)` continues the conversation without a Session.
- `text`: decoded lazily, in this order: user-specified → BOM → HTTP charset → `<meta>` / XML declaration in the first 4096 bytes → strict UTF-8 → encoding detection → configurable default (`response.default_encoding`, UTF-8 unless set).
  - A declared encoding (HTTP charset or document) is used only if the body decodes with it strictly, apart from an incomplete character cut off at the end; otherwise the next step is tried. Undecodable bytes are replaced.
  - Encoding labels are mapped following the WHATWG Encoding Standard; for example, both `gb2312` and `gbk` are decoded as `gb18030`, and `iso-8859-1` as `windows-1252`. An explicit encoding or the default may also be a Python codec name.
  - The codec actually used is stored in `encoding`, and the step that chose it in `encoding_source`: `explicit`, `bom`, `header`, `document`, `utf-8`, `detected`, or `default`.
- Selectors: `css()`, `xpath()`, `re()` / `re_first()` over the whole text, and `jmespath()`, whose results keep JSON values as they are. The document is parsed only on first call: XML media types as XML, JSON media types as JSON, and anything else as JSON if it is an object or array, as XML if it starts with an XML declaration, and as HTML otherwise. `json()` decodes the text with orjson and caches the decoded value.
- Links: `urljoin()` resolves a URL against the Response's base URL (final URL, honoring the first `<base href>` of an HTML document unless browsers would ignore it, such as a `data:` or `javascript:` base), for example to store absolute links in Items. There are no `follow` helpers: Requests are always written out explicitly, and relative URLs are resolved automatically (see §4.3).
- Responses from the browser also carry `response.browser`: captured XHRs, screenshots, and action outputs; with `keep_page` enabled there is also `response.page`, see §4.7.

### 4.5 Callbacks and output semantics

- A Callback can be an `async def` async generator that yields `Request` objects or Items (None is ignored), or a plain `async def` that returns a single object, a list, or None.
- **Output is streamed**: each yielded object is processed immediately. Requests go to the Frontier; Items go into a bounded queue, and when the queue is full the Callback is suspended (backpressure).
- **When a Callback raises** (confirmed):
  - The Request is marked as failed: it is kept as a Dead Request, `RequestFailed` is emitted, and the errback is called. It is **not retried**, because parsing errors are usually deterministic; this holds for every exception a Callback raises, including one from an Inline Fetch, such as `RetriesExhausted` once its own retries ran out.
  - Output yielded before the exception is kept.
  - To retry, the user explicitly writes `raise Retry(reason, delay=None)`; the retry policy still decides whether a retry is left (§6.4). Deciding whether a page is Blocked is covered in §5.4 and handled automatically by the framework.
- **Errbacks**: `errback(request, error)` runs once a Request fails for good, whether its fetch failed, its retries ran out (`error` is then `RetriesExhausted`, carrying the last `response` or the last exception as its cause), or its Callback raised. Like a Callback, it may yield Requests and Items; relative URLs resolve against the failed Request's URL. An errback that raises is logged.
- Every Request must go through the Frontier; a Callback cannot bypass scheduling and hand a Request directly to a Fetcher. For cases that need the Response "right away", use the Inline Fetch in §4.6.

### 4.6 Inline Fetch (`await self.fetch`)

```python
async def parse_detail(self, response):
    api = await self.fetch(Request(comments_api(response)))   # also accepts a URL string
    yield {"title": response.css("h1::text").get(), "comments": api.json()["data"]}

async def parse_report(self, response):
    async with self.stream(response.css("a.pdf::attr(href)").get(), max_bytes=0) as pdf:   # the body as it arrives
        await pdf.save(f"reports/{response.meta['id']}.pdf")   # or: async for chunk in pdf.iter_bytes()
```

- Use cases: logging in, exchanging for a Token, paging sequentially through a paginated API, cases such as "detail page + comments API" where the output has to be assembled within one Callback, and downloading files.
- `self.fetch()` and `self.stream()` call `Crawler.fetch()` and `Crawler.stream()`, so components given the Crawler can fetch inline too. They work while the Run runs, from `SpiderOpened` to `SpiderClosed`: in `start()`, `on_idle()`, Callbacks, errbacks, and components; elsewhere they raise `RuntimeError`. A URL string is fetched with a GET, and a relative one resolves against the Response being handled. A Request's `callback`, `errback`, `priority`, and `dont_filter` play no part.
- Semantics:
  - Goes through the same download Middleware chain and the same rate limiting: each attempt waits for its domain's slot, and waits out a Cooldown in place. It runs in the task of the Request being handled and takes no worker of its own, so Inline Fetches run concurrently, such as with `asyncio.gather`, are limited by `domain.concurrency` alone.
  - A fetch nested in another for the same domain, in the same task, such as one a download Middleware makes to get a token, shares the outer fetch's place in the slot instead of waiting for another, which the outer fetch could hold forever. It still waits for the domain's delay and rate, and waits out a Cooldown. Tasks the outer fetch starts need places of their own.
  - Uses the Session of the fetch it is nested in, such as one a download Middleware makes, or of the current Request, if it has one, unless it names its own; otherwise it leases one like any Request when a Session Pool is enabled, and without one it is independent and sends only the cookies passed to it.
  - **Does not enter the Frontier, is not deduplicated, and is not persisted.**
  - The retry policy judges each attempt as usual, but a retry waits in place for its delay, emitting `RequestRetried`. The Response is returned whatever its status, unless the policy retries it. Once the retries run out, `RetriesExhausted` is raised, holding the last Response or caused by the last exception, as errbacks receive it; an exception the policy does not retry, such as a `TLSError`, or `IgnoreRequest` from a Middleware, is raised as it is. Uncaught, it fails the Callback (§4.5).
  - It is counted in `downloads.*`, but not emitted as `ResponseReceived`, which is for Responses handed to Callbacks, nor as `RequestFailed`: the error goes to the caller. Its log records carry the `request_id` of the Request being handled.
  - When the parent Request is cancelled, as by a Force Stop, its Inline Fetches are cancelled with it and their transfers stopped; a Drain lets them finish within its grace period.
- **Streaming**: `self.stream(request, max_bytes=None)` gives a `StreamedResponse` whose `response` holds everything but the body, read once with `iter_bytes()` or written with `save(path)`; leaving `async with` closes the transfer. `max_bytes` defaults to the Fetcher's limit, such as `http.max_body_size`, and 0 sets none.
  - Download Middleware see the Response marked `streamed`, with an empty `body`; Middleware that read bodies, such as caches, should pass those through. A Middleware may answer with another Response, such as a cached one, whose body is then read instead; one that fetches more than once reads the last transfer, the earlier ones closed. A Fetcher without `stream()` reads the whole body first.
  - Only the attempts up to the response headers are retried; a failure while reading the body is raised from `iter_bytes()`.
  - The stream holds its domain's slot until it is closed, so `domain.concurrency` counts the transfers open. Fetches made in the block, in the same task, share the slot as nested fetches do.
  - A slow reader keeps about 1 MiB of the body in memory, with or without `max_bytes`: the Fetcher stops receiving until the reader catches up, and a paused transfer never times out. Streamed bytes count toward `downloads.bytes` as they are read.

### 4.7 Render and page interaction

Browser rendering is expressed with the `render=` parameter (confirmed). Since it is the same Request type, a Middleware can upgrade a Request to Render with `request.replace(render=True)`; custom transports are still selected with `fetcher=`.

```python
from pernix.browser import Render, Scroll, Click, WaitFor

yield Request(
    url,
    callback=self.parse_list,
    render=Render(
        wait_for="div.item",                       # wait for a selector
        actions=[Scroll(times=3), Click("button.more"), WaitFor("div.loaded")],
        block=("image", "media", "font"),          # resource types to block; none by default
        capture="**/api/list*",                    # capture matching XHR responses
        screenshot=False,
    ),
)
yield Request(url, render=True)                    # render only, no extra actions
```

Page interaction comes in three layers; use whichever you need:

1. **Declarative actions**: serializable, persisted along with the Request, and covering the most common operations (wait, scroll, click, type, run JS, screenshot).
2. **Page methods**: `Render(setup=self.before_goto, action=self.after_load)`. The arguments are Spider methods with the signature `async def x(self, page)` and can run arbitrary patchright code. If one raises, the Request fails (each can be marked optional individually).
3. **Kept page**: `Render(keep_page=True)`; the Callback gets the live `Page` through `response.page`. When the Callback ends (normally, with an exception, or by timeout), the framework **force-closes** the page and releases the concurrency slot.

Other conventions:

- The rendered HTML becomes `body`; the raw network-level response is available from `response.browser`.
- Pages are closed by the Fetcher. Pages that timed out or were cancelled are treated as "dirty pages" and are always closed directly, never reused.

### 4.8 Items and Pipelines

- The framework **does not build its own Item/Field system**. dicts and other mappings, dataclasses, msgspec `Struct`s, pydantic models, and attrs objects can all be Items; `Request` and `Response` are not. pydantic and attrs are only consulted when the application imported them. `pernix.items.item_to_dict()` converts any Item to a plain dict for export, nested Items included; msgspec honors `rename`, and pydantic uses field names. For validation, use pydantic or msgspec models directly.
- To store different kinds of Items apart, such as in different tables, give each kind its own type and route by type. dicts can be routed by content, with an `accepts` function.

```python
from pernix import DropItem, ItemContext, Pipeline

class Clean(Pipeline):
    async def process(self, item, ctx: ItemContext):   # sync works too
        if not item["title"]:
            raise DropItem("empty title")      # returning None also drops the Item
        return item                            # or a new object, passed on to the next Pipeline

class SaveRows(Pipeline):
    accepts = (Book,)                          # optional: only these types; or a function (item) -> bool
    batch_size = 500                           # optional: enable batch mode
    batch_timeout = 1.0                        # a batch waits at most this long after its first Item
    retry_times = 3                            # optional: retry this Pipeline's call on transient errors
    retry_on = (ConnectionError, TimeoutError) # which errors are retried; raising Retry also retries
    timeout = 300                              # optional: seconds per call, instead of items.timeout
    def __init__(self, settings: DbSettings) -> None: ...   # dependencies are injected
    async def open(self) -> None: ...
    async def process_batch(self, items, contexts) -> None: ...
    async def close(self) -> None: ...
```

- Pipelines are registered with `pipelines = {pipeline: order}` and run in order; `open()` / `close()` follow the component lifecycle, as for Extensions. `ItemContext` holds the `request` and the `response` an Item came from (`None` for an errback's Items).
- Callbacks hand Items to a bounded queue (`items.queue_size`, 1000); when it is full, the Callback is suspended until there is room (backpressure). `items.concurrency` (1) Items are processed at once; with 1, Items reach the Feeds in the order they were produced, as an Item a batch Pipeline does not accept waits behind the Items pending there.
- Outcomes: an Item that passes every Pipeline is written to the Feeds that accept it and emitted as `ItemScraped`. Returning `None` or raising `DropItem` emits `ItemDropped`. Any other exception emits `ItemError` and drops the Item; the Request is never fetched again for it.
- Failed Items: each Item a Pipeline failed on (retries exhausted included, drops not) goes, as a `FailedItem(item, pipeline, error)`, through the failure Pipelines of `failed_pipelines = {pipeline: order}`, which are Pipelines without batch mode: each passes the `FailedItem` on, or stops it with `None`. The built-in `FailedItemsFile`, registered by default, writes each one with the Pipeline, the error, and the URL to the JSON-lines file of `items.failed_output`, so it can be processed again later; without that setting it does nothing. A failure Pipeline that fails loses the failed Item: it is logged and counted as `items.failures_lost`, unless it is a `FeedError`, which ends the Run.

```python
class DeadLetters(Pipeline):
    retry_times = 3
    async def process(self, item: FailedItem, ctx: ItemContext):
        await self.producer.send(topic, item.item, error=str(item.error))
        return item                            # on to the next failure Pipeline

settings = {"failed_pipelines": {DeadLetters: 200}}   # next to the built-in FailedItemsFile (100)
```
- Batch mode: `process_batch(items, contexts)` runs when the batch is full (in the worker that filled it, so a slow batch applies backpressure), when `batch_timeout` passes, when as many Items it does not accept wait behind it as the Item queue holds, and when the Run ends. The Items then pass on unchanged; `DropItem` drops the whole batch, and another exception fails each of its Items. Batches of one Pipeline never overlap.
- Timeouts: one call of a Pipeline, `process()` for an Item or `process_batch()` for a batch, may take `items.timeout` seconds (60 by default; 0 for no limit), or the Pipeline's own `timeout`. Then it is cancelled and fails with `TimeoutError`, so a hanging database call cannot stall the crawl. Only async calls can be cancelled; blocking work belongs in async code or `asyncio.to_thread`.
- Retries: a Pipeline with `retry_times` retries its own call (one Item, or a whole batch) when it raises one of `retry_on` (by default `OSError` and `TimeoutError`, timeouts included) or `Retry`, with full-jitter backoff from 1 s up to 60 s, scaled by `retry.delay_scale`; each attempt has its own timeout. Meanwhile the Items wait, so an unavailable database slows the crawl down instead of losing data. Once the retries run out, the error is `RetriesExhausted`; without retries, the error is kept as it is. `DropItem` and `FeedError` are never retried. `items.retried` counts the retries.
- Delivery semantics: in M0, a Request's Lease is acknowledged once its Items are queued. With the in-memory Frontier nothing survives a crash anyway. With persistent Frontiers (M2), acknowledgment will wait until the Request's Items are processed, making delivery at-least-once: output may be produced again after crash recovery, so Pipelines should write idempotently, such as by upserting on a business key. The framework does not promise exactly-once.
- When the Run ends, the queued Items and the pending batches are processed before `SpiderClosed`. A cancelled Run abandons the queued Items.
- Four writer Pipelines are built in, for MySQL, PostgreSQL, MongoDB, and Kafka, and are installed as optional extras (confirmed); the database ones support batch writes and upserts by business key.

**Feeds** write Items to files: `output=` / `-o books.jsonl`, and the `feeds` setting.

```python
settings = {"feeds": {
    "out/{name}-{time}.jsonl": {},
    "books.csv": {"fields": ["title", "price"], "accepts": [Book]},
}}
Books.run(output="books.json")   # adds {"books.json": {}}
```

- Formats: jsonl (orjson), csv, and json (an array streamed one Item per line), chosen with `format` or by the extension (`.jsonl`, `.ndjson`, `.jl`, `.csv`, `.json`). `fields` selects and orders the fields; for csv they are the columns, which otherwise come from the first Item. `accepts` restricts a Feed to some Items, like a Pipeline's. `encoding` defaults to UTF-8.
- Paths are templates with `{name}` (the Spider), `{run_id}`, and `{time}` (when the Run started, in UTC, such as `20261013T080405Z`). Unknown placeholders, unknown extensions, and two outputs that would write the same file (Feeds, `output=`, or `items.failed_output`) fail at startup. Missing directories are created.
- Files are written to a hidden temporary file next to the target and renamed over it when the Feed closes, also when the Run fails, so a file appears complete or not at all, and an existing file is replaced.
- Values JSON cannot represent, such as `bytes`, fail the write; `Decimal`s are written as strings, sets as lists. In csv, `None` is an empty cell, booleans and containers are JSON, and dates are ISO 8601. A Feed that fails to write raises `FeedError`, which emits `ItemError` and ends the Run, rather than lose Items silently.

### 4.9 Middleware

Download Middleware uses the onion model:

```python
from pernix import Middleware, IgnoreRequest, middleware

class SignRequest(Middleware):
    async def __call__(self, request, call_next):
        request = request.replace(headers=sign(request))
        response = await call_next(request)    # not calling = short-circuit (e.g. a cache); calling more than once = retry
        if response.status == 401:
            ...
        return response

@middleware                                    # can also be written as a function
async def timing(request, call_next): ...
```

Spider Middleware wraps around the Callback and takes the form of an async generator:

```python
class DropEmpty(SpiderMiddleware):
    async def __call__(self, response, call_next):
        async for out in call_next(response):
            if out:
                yield out
```

- Registration: `middlewares` and `spider_middlewares` are `{component: order}` maps; a lower order is further out. A spider Middleware can also be written as a function with `@spider_middleware`.
- Middleware are components like Extensions: services are injected, `events.on()` methods are subscribed, and they are opened and closed with the Run. The chains are built once at startup, and a Middleware that is not an async callable (download) or an async generator (spider) is rejected then, before anything is opened.
- On a single chain, the order in the request direction and in the response direction is naturally symmetric, and exceptions propagate naturally along the chain; there is no asymmetry such as "exceptions raised in the response phase bypass exception handling". Only `Exception` is caught by the framework, so `CancelledError` always propagates.
- A spider Middleware may pass another Response to `call_next`; exceptions raised by the Callback come out of its `async for`, so it can catch them. Requests constructed in a layer, or in the Callback, resolve relative URLs against the Response that layer, or the Callback, received. When a layer ends, even early, the inner layers and the Callback it started are closed with it.
- Control flow is expressed with exceptions such as `IgnoreRequest`, `Retry`, and `DropItem`, which are not `PernixError`s. `IgnoreRequest` raised by download Middleware or a Fetcher drops the Request without counting it as failed and emits `RequestIgnored(reason)`; `Retry` asks the retry policy for another attempt (§6.4); `DropItem` comes with Pipelines.
- Retries are not a Middleware: the Engine asks the retry policy after each attempt and returns the Request to the Frontier, so every Middleware runs again on each retry (§6.4).
- A `NodeMiddleware` adapter is also provided, which wraps `process_request` / `process_response` / `process_exception`-style classes into the onion to make migrating existing code easier. The hooks may be plain or `async` methods:
  - `process_request(request)` returns `None` to continue inward, or a Response that skips the inner layers.
  - `process_response(request, response)` returns the Response or a replacement; it also sees Responses returned by the other two hooks.
  - `process_exception(request, exception)` sees exceptions from the inner layers and returns `None` to let them propagate, or a Response.
  - Any hook may return a Request instead, which is fetched in place of the original, starting again at this layer; it does not go back to the Frontier.

### 4.10 Events and Extensions

```python
from pernix import Extension, events

class Notify(Extension):
    def __init__(self, crawler: Crawler) -> None: ...   # injected, see §4.11
    async def open(self) -> None: ...                   # before the Run
    async def close(self) -> None: ...                  # after the Run, in reverse order

    @events.on(events.SpiderClosed)
    async def closed(self, event: events.SpiderClosed) -> None: ...

class Books(Spider):
    settings = {"extensions": {Notify: 100}}

unsubscribe = crawler.events.on(events.ItemScraped, handler, priority=0, timeout=5)   # dynamic subscription
```

Events are frozen dataclasses. Each Event class declares how it is dispatched, and is dispatched with the matching method of the event bus:

| Dispatch | Method | Execution | Use case |
| --- | --- | --- | --- |
| `observe` | `await bus.emit(event)` | Subscribers run in order; one that raises or times out is logged and isolated, and return values are ignored | Most notification Events |
| `gate` | `await bus.gate(event)` returns a `Veto` or `None` | Subscribers run in order until one returns `Veto(reason)`. A subscriber that raises or times out is ignored (fail-open), or vetoes if the Event sets `fail_closed = True` | For example, admission to the Frontier |
| `transform` | `await bus.transform(event)` returns the Event | Each subscriber may return a replacement Event of the same class, or `None` to keep it; failures and wrong results are logged and discarded | Events whose data is rewritten layer by layer |

Other conventions:

- Subscribers run in `(priority, registration order)`; Events that set `reverse = True`, such as `SpiderClosed`, run them in reverse. A subscription to a class also receives its subclasses. Dispatch takes a snapshot of the subscribers.
- Both sync and async subscribers are supported, and a returned awaitable is always awaited. An async subscriber is cancelled after its own timeout, or `events.timeout` (30 s by default). `CancelledError` always propagates.
- `events.on()` marks methods; the Crawler subscribes the marked methods of every component it builds.

Core Events:

| Event | Dispatch | When |
| --- | --- | --- |
| `SpiderOpened` | observe | Components are open; nothing has been fetched yet |
| `SpiderIdle` | observe | Nothing is ready, delayed, or leased, and `start()` is exhausted; `Spider.on_idle()` runs next |
| `SpiderClosed(reason)` | observe, reverse | The Run ended: `finished` on its own, `drained` after a Drain, `forced` after a Force Stop (§6.6), `failed` by an error, or `cancelled` from outside |
| `RequestScheduled` | gate (fail-open) | A Request is about to enter the Frontier |
| `RequestDropped(reason)` | observe | A Request was vetoed, or was a duplicate |
| `RequestRetried(attempt, delay, reason)` | observe | An attempt did not succeed, and the Request goes back to the Frontier for another |
| `RequestFailed(error)` | observe | A Request failed for good: an exception the retry policy keeps, its Callback raised, or its retries ran out (`RetriesExhausted`); its errback runs next |
| `RequestIgnored(reason)` | observe | Download Middleware or a Fetcher raised `IgnoreRequest` |
| `ResponseReceived` | observe | A Response was fetched, before its Callback runs |
| `ItemScraped` / `ItemDropped` / `ItemError` | observe | An Item passed the Pipelines and was written to the Feeds, was dropped, or failed; `response` is `None` for an errback's Items |
| `StatsSnapshot(stats, final)` | observe | A copy of the stats: every `stats.interval`, and a final one after `SpiderClosed` |

Lifecycle: Extensions (the `extensions` setting) are built in order, subscribed, and opened, as async context managers or through `open()`. They are closed in reverse after `SpiderClosed`; `__aexit__` receives the exception that ended the Run, so a component can roll back, but cannot suppress it. Every close step runs even when earlier ones fail or are cancelled: failures are raised together as an `ExceptionGroup`, and a cancellation is re-raised after the last component is closed. If opening a component fails, the ones already open are closed in reverse. Once `SpiderOpened` has been dispatched, `SpiderClosed` always follows, even on cancellation.

**Addons** bundle settings changes and registrations:

- **Enabling**: an Addon is enabled explicitly with the `addons` setting, by entry point name (group `pernix.addons`), import path, class, or object. An installed Addon that is not listed is never loaded.
- **Compatibility**: an Addon declares `api_version`, a version range such as `">=1,<2"`; this Pernix provides Addon API `1.0`.
- **Settings**: `update_settings(settings)` contributes the Addon layer of the settings, below the project file. The `addons` setting is read first, ignoring keys that only the Addons' components declare.
- **Setup**: the synchronous `setup(api)` may only register, for example `api.on(...)`, and only during the call; resources belong in an Extension's `open()`.

### 4.11 Settings

Layers, from lowest to highest precedence:

1. Built-in defaults
2. Addons
3. Project file (`pernix.toml`, or `[tool.pernix]` in `pyproject.toml`), found by walking up from the working directory; the nearest `pyproject.toml` ends the search
4. Environment variables `PERNIX_*`
5. `Spider.settings`
6. The `settings=` argument of `run()`, `crawl()`, and `stream_items()`
7. `-s KEY=VALUE` on the command line, parsed like environment variables

```python
class Books(Spider):
    settings = {"concurrency": 32, "domain.delay": 0.5}        # dotted keys

Books.run(output="books.jsonl", settings={"concurrency": 8})   # run arguments
```

```toml
# pernix.toml
concurrency = 32

[domain]                                   # nested tables are the same as dotted keys
delay = 0.5

[middlewares]
"myproject.middlewares.Sign" = 500
"myproject.middlewares.Legacy" = false     # TOML has no null: false disables

[fetchers]
s3 = "myproject.fetchers.S3Fetcher"        # registered by name; Request(fetcher="s3")
```

Rules:

- Keys are lowercase with dot-separated namespaces, for example `concurrency`, `domain.delay`, `http.impersonate`, `browser.headless`, `retry.times`. Nested mappings and dotted keys are equivalent.
- Environment variables map `PERNIX_DOMAIN__DELAY` to `domain.delay`; their string values are parsed by the declared type, and container values are written as JSON. Values from Python layers are type-checked strictly.
- Settings are merged and validated at startup. Every problem is reported at once: a misspelled key suggests the closest known key, and a wrong value names the layer it came from (for example the environment variable). After startup the settings are immutable, and every Crawler has its own instance.
- Settings are declared as typed **Sections**: a key prefix plus fields with defaults and optional constraints. `settings.section(RetrySettings)` returns the validated values.
- Components (Middleware, Pipelines, Extensions) are registered as `{component: order}`, lowest order first. A later layer adds or overrides entries, and `None` (`false` in TOML and JSON) disables one. Fetchers are registered by name instead, as `{name: component}`, merged name by name. A component can be given as a class, an object, or an import path.
- A component registers its own Sections by annotating `__init__` parameters with them (which also injects them) or by listing them in `settings_sections`; their keys are validated together with the core keys. Sections of a disabled component stay registered, so its settings can remain in the project file.
- Construction: an object is used as is; a class with `from_crawler(crawler)` is built through it. Otherwise, `__init__` parameters annotated with exactly `Crawler`, `Settings`, `EventBus`, `Stats`, or a registered Section are injected, and other parameters keep their defaults. These types may be imported only under `TYPE_CHECKING`. There is no global service locator.
- Secret settings use the `Secret` type and are automatically redacted in reprs, logs, and stats.

### 4.12 Running and the CLI

```python
result = Books.run(output="books.jsonl", settings={"concurrency": 32})  # synchronous entry point, the main program of a script
result = await Books.crawl(keyword="x")               # run inside an existing event loop; keyword arguments are Spider arguments
async with Books.stream_items(keyword="x") as items:  # bounded streaming consumption, suited to embedding in other applications
    async for item in items:
        ...
```

Keyword arguments of `run()`, `crawl()`, and `stream_items()` are Spider arguments (see §4.2); settings for the Run are passed in `settings=`, and an extra Feed in `output=`. The `run_id` is the `run_id` setting, so it can come from `settings=`, from `PERNIX_RUN_ID`, or from the CLI; otherwise one is generated.

- `run()` and `crawl()` return a `RunResult(run_id, reason, stats)`, where `reason` is `finished`, `drained`, or `forced`, and `stats` is the final snapshot.
- `run()` behaves as a program: it creates its own event loop, installs the stop signals (§6.6) in the main thread, and ends the process when the Run does not complete. A configuration error (invalid settings, Spider arguments, or components, all found before anything is opened) is written to stderr, or logged by the Run, without a traceback, and exits with 2; a failed Run is logged with its traceback and exits with 1; a Run stopped by a signal exits with 130, whether it drained or was forced. A Run that finished, or that code stopped, returns.
- `crawl()` and `stream_items()` are library calls: errors are raised, signals are left to the application, and cancelling the task stops the Run like a Force Stop (`cancelled`). For finer control, `Crawler.from_spider(Books, args=..., settings=...)` gives the Crawler, with `crawl()`, `stream_items()`, and `stop()`.
- `stream_items()` yields the Items that pass every Pipeline, the same ones the Feeds receive, holding one at a time. A slow consumer therefore pauses the Item processing, then the Callbacks once the Item queue is full, and so the fetching. The Run starts with the first iteration; if it fails, the error is raised after the Items already received. Leaving early (`break`, an exception, leaving `async with`) force-stops the Run: `async with` or `aclose()` waits for it to stop, while an abandoned stream stops once it is garbage-collected.

| Command | Purpose |
| --- | --- |
| `pernix run TARGET[:Name] -a k=v -s key=val -o out.jsonl --run-id ID` | Run a Spider, as `Spider.run()` does |
| `pernix fetch URL [--fetcher NAME] [--timeout S] [--json] [-o FILE]` | Fetch once and print the Response |
| `pernix shell [URL]` | Interactive console with top-level `await`, and `request`, `response`, `settings`, and `fetch()` preloaded |
| `pernix check [TARGET]` | Validate settings and components (and, with the browser Fetcher, the browser environment) |
| `pernix run ... --job-dir DIR` | Resume a Run (planned, with persistent Frontiers) |
| `pernix new NAME` | Generate a project skeleton (planned; project mode does not enforce a directory layout) |
| `pernix browser install` | Install browser engines (planned, with the browser Fetcher) |

- Every command takes `-s KEY=VALUE`, the highest settings layer (§4.11). `run` and `check` take `-a KEY=VALUE` Spider arguments. Results go to stdout; messages and logs go to stderr.
- TARGET is a `.py` file, imported under its stem with its directory first on `sys.path`, or an import path such as `myproject.spiders.books`. As with `python -m`, the working directory comes first on `sys.path`, so the project's own modules, Spiders and components alike, import without being installed. `:Name` picks a Spider by class name or `Spider.name`; it may be left out when the module defines exactly one Spider, not counting those it imports.
- Exit codes are those of `run()`: 0 completed, 1 failed, 2 configuration error (including invalid options, and a TARGET that cannot be found), 130 stopped by a signal.
- `fetch` and `shell` fetch through the download Middleware and the Fetchers of the project settings, without retries, a Frontier, or Pipelines. `--fetcher` names a registered Fetcher, such as `browser`; otherwise the URL scheme picks one. Impersonation is on by default with the pinned Profile (§5.4); `-s http.impersonate=...` changes it.
- `fetch` writes the status line, the redirects, and the headers to stderr, and the body to stdout: decoded text for text media types, raw bytes otherwise, though never to a terminal. `-o FILE` writes the body byte for byte. It exits with 0 for any Response, whatever its status, and 1 when fetching failed.
- `fetch --json` writes one JSON object instead: `url` (requested), `final_url`, `status`, `http_version`, `headers` as `[name, value]` pairs in order, `encoding`, `elapsed` in seconds, `redirects` as `{url, status}` objects, `fetcher`, `body` (the decoded text, or `null` for a binary body or one written with `-o`), and `body_bytes`. A failure is `{"url": ..., "error": {"type": ..., "message": ..., "code": ...}}`, where `code` is the transport's error code, such as libcurl's.
- In `shell`, `await fetch(url_or_request)` returns the Response and rebinds `request` and `response`; Ctrl-C cancels what is being awaited, and Ctrl-D leaves.
- `check` builds every component, including the `setup()` of the Addons, without opening any, so import paths, component roles, chains, and Feed paths are all checked, and prints a summary of the project file, the Spider, and the components. Every problem is reported at once: the settings are validated together, and so are the components that do not depend on one another, as when a Run starts.

## 5. Fetching layer

A Fetcher is a component with `async def fetch(self, request, *, session=None) -> Response` and a `schemes` attribute, the URL schemes it fetches by default. It may also have `stream(request, *, max_bytes, session=None)`, an async context manager giving a `StreamedResponse`, for streamed Inline Fetches (§4.6). `session` is the Session the Request is sent under, which the Session Pool passes only for such Requests (§5.3): a Fetcher that never sends Requests under a Session may leave the parameter out, and one that cannot refuses them. Fetchers are registered by name with the `fetchers` setting, `{"http": HTTPFetcher, "browser": BrowserFetcher}` by default; registering another component under a built-in name replaces it, and `None` (`false`) disables one. The innermost step of the download chain picks the Fetcher: the one named by `Request(fetcher=...)`, otherwise `browser` if the Request needs rendering, otherwise the one whose `schemes` include the URL's scheme. No two Fetchers may claim the same scheme, and a Request no Fetcher fits fails. `Response.fetcher` records the name. Like other components, Fetchers get services injected and are opened and closed with the Run.

### 5.1 HTTP Fetcher (curl-cffi)

The `HTTPFetcher`, registered as `http`, registers the `http.*` settings.

**curl session pool**

- Underlying curl sessions are grouped and reused by `(Session, Profile)`, Requests without a Session sharing the ones of their Profile, created on first use inside the running event loop, and evicted by LRU beyond `http.pool_size` (8) plus an idle timeout of `http.pool_idle` (300 s); a session in use is never closed, one idle too long is replaced rather than reused, and retired sessions close in tasks of their own, which closing the Fetcher waits for. A single underlying curl session never mixes different Profiles or Sessions, so connections are never shared between them.
- impersonate is set explicitly on every Request, so a reused connection handle never carries over the previous Request's Profile. The Profile is `http.impersonate`, or `Request(impersonate=...)`; both must name a pinned target (§5.4).
- Cleartext `http://` Requests use HTTP/1.1, as browsers never upgrade those to HTTP/2; HTTPS negotiates as the Profile does.

**Timeouts and cancellation**

- `http.timeout` (30 s), or `Request(timeout=...)`, limits connecting and waiting for the response headers, precisely, with asyncio, and then each pause in the body, to the nearest second, with curl. `http.deadline` (180 s) limits the whole fetch, waiting for a connection and reading the body included, so a server trickling data cannot hold a worker forever.
- Every transfer is streamed from curl. A fetch that is cancelled, times out, or stops reading early, before or after the headers arrived, stops its transfer: curl drops the handle and its unfinished connection, and the pool stays usable.
- curl-cffi exceptions are mapped onto the framework's own exception hierarchy, `FetchError` and its subclasses `Timeout`, `ConnectError`, `ProxyError` (a 407 response from a proxy also raises it), `TLSError`, `TooManyRedirects`, `DecodeError`, and `ResponseTooLarge`; the original curl error code is kept in `code` (0 for limits the framework enforces), and the curl-cffi exception is the `__cause__`.

**Concurrency**

- Per-host connection limits are implemented by the framework's concurrency slots, not by curl's multi-handle options; each curl session allows `concurrency` transfers in flight.
- HTTP/2 connection reuse is enabled (`PIPEWAIT`).

**Data**

- Request headers preserve order and case, and support duplicate keys; they follow the Profile's own headers.
- Response headers are read with all multiple values preserved. A body decoded from gzip, deflate, br, or zstd loses `Content-Encoding` and `Content-Length`, which describe the encoded body; a `HEAD` Response keeps them.
- Redirects are followed up to `http.max_redirects` (20, as browsers do), and recorded in `Response.history`.
- International hosts: curl converts a host to ASCII itself, differently on each platform (on Linux, libidn2's older Unicode tables, retrying with the transitional mappings when the non-transitional conversion fails), so before curl sees a Request's URL, the Fetcher converts the host curl connects to, the one after the last `@` of the authority, when it is not ASCII or holds percent escapes, as WHATWG URLs convert hosts (`pernix.urls.with_ascii_host()`): percent-decoded, without the transitional IDNA mappings, and IPv4 addresses in dotted decimal. Every platform then reaches the host a browser does, `STRAẞE.de` being `xn--strae-oqa.de`, not `strasse.de`, and `Response.url` names it in ASCII. A host WHATWG URLs reject, such as `a<ZWJ>b.example`, fails the fetch before connecting with a `FetchError` (code 3, as curl's own malformed URLs), which is not retried, as browsers refuse such a URL. curl still converts the hosts of redirects.
- A body larger than `http.max_body_size` (100 MiB; 0 for no limit) fails with `ResponseTooLarge`: at once when `Content-Length` declares it, unless the response has no body (`HEAD`, 1xx, 204, 304), otherwise as soon as curl receives more, counted from the first byte in curl's write callback, however slowly the body is read.
- Streaming downloads: `async with fetcher.stream(request, max_bytes=...) as streamed:` gives a `StreamedResponse` whose `response` holds everything but the body, marked `streamed`, read once with `iter_bytes()` or written to a file with `save(path)`. `http.deadline` then limits only the wait for the headers. curl stops receiving while 1 MiB of the body waits to be read, and resumes once half of it has been, so a slow reader holds a bounded part of the body in memory, whatever `max_bytes` is; a paused transfer never times out. The chunks curl keeps while paused count once toward `max_bytes`. Callbacks reach it with `self.stream()` (§4.6).

**Delegated to the framework**

- Without a Session, no cookies persist between Requests (ADR 0011): each transfer starts with only the Request's cookies, and cookies set during it, including on redirects, go to `Response.cookies` instead of shared state. Connections are still reused. With a Session, its cookies are managed by the framework, see §5.3.
- A Request sent under a Session also sends the Session's cookies: they are given to curl's cookie engine with their domains, host-only flags, paths, Secure flags, and expiry, so curl matches them to each redirect hop, and a cookie of the Request held for its host and the path `/` replaces the Session's. As the headers of each hop arrive, so even when a later hop or the body fails, its `Set-Cookie` headers are applied to the Session's cookie jar, with the hop's URL and the time they arrived, by the jar's own rules, so attributes curl does not keep, such as `SameSite`, are kept; those of informational (1xx) responses and of a proxy's answer to `CONNECT` are not applied, and the Request's own cookies are not kept. The Session's proxy and Profile apply unless the Request names its own, and `Response.session` is the Session's id. curl orders the cookies by domain length and name length too, unlike a browser.
- The Request's `cookies=` are given to curl's cookie engine as host-only cookies of its host, for every path, naming the host the Fetcher gives curl (lower-cased, IPv6 without brackets, and in ASCII, so `faß` is `xn--fa-hia`): they go to every redirect hop on that host, whatever its port or scheme, and to no other host, and cookies set on the way join them as curl handles them. curl orders the cookies it sends by name length, longest first, and the newest first among names of one length, so the Fetcher loads them in reverse: names of one length keep the order given, but a browser would keep it for all of them. Over HTTP/1.1 they form one `Cookie` header, and over HTTP/2 each is a `cookie` field of its own, as Chrome sends them. Names with the `__Secure-` or `__Host-` prefix are Secure, as browsers and curl require, so they go over HTTPS, and to loopback hosts, only. A `Cookie` header given in `headers` is sent as given, and curl drops it on a redirect to another host, port, or scheme.
- Retries are handled by the framework; curl-cffi's built-in retries are turned off, and so are proxies from the environment, `NO_PROXY` included, so it never bypasses a proxy a Request names.

### 5.2 Browser Fetcher (patchright)

**Topology**

- The browser Fetcher starts patchright with the first Request that needs rendering, and it launches one browser with `launch()`, so a Run that renders nothing never starts them; both are closed with the Run. Without `pernix[browser]` installed, or without the browser engine, that Request fails with a `ComponentError` naming the command that installs it, and so do the later ones, at once.
- The browser is the Chrome for Testing build bundled with the pinned patchright, never the headless shell. It creates multiple contexts, one per Session; until those exist, a Request under a Session is refused. Without a Session, each rendered Request gets a fresh context, closed after the fetch, so Requests stay independent as on the HTTP side.
- A context's proxy, storage state, locale, and time zone are set once at creation and never modified afterward; switching proxies means creating a new context. `Request(proxy=...)` is the proxy of its context, for every request of it, loopback ones included.

**Loading a page**

- A page is loaded until its `load` event, within `browser.timeout` (30 s), or `Request(timeout=...)`; `browser.deadline` (180 s) limits the whole fetch, starting the browser included. A fetch that is cancelled or times out closes its context in a task of its own, which closing the Fetcher waits for.
- The cookies a Request carries (`cookies=` or a `Cookie` header) are added to its context as host-only cookies of its host, for every path, in the order given: they go to every redirect hop on that host and to no other host, and cookies set while the page loads flow as in the browser. The browser takes cookies named with the `__Secure-` or `__Host-` prefix only as Secure ones, so those go over HTTPS, and to loopback hosts, only.
- A Request other than a plain GET, or with headers, changes the first request of the page: its method, body, and headers. The browser keeps those headers on the redirects that follow, to any host, as the HTTP Fetcher does, so the browser Fetcher refuses `Authorization` and `Proxy-Authorization` headers, which the HTTP Fetcher drops on the way to another host. The method and body follow the browser's redirect rules, and the page's own requests are untouched.
- The body of an HTML or XHTML document is its rendered DOM, encoded as UTF-8 and decoded as UTF-8, without the `Content-Length` and `Content-Encoding` that describe the body sent. Chrome shows other documents, such as JSON, text, or images, in a viewer page of its own, so their body is the one received. A document the browser downloads instead of showing fails with a `FetchError`, and a 204 or 205 gives an empty body.
- `status` and `headers` are those of the document as the browser reports them: every value is kept, but not their wire order. `history` holds the redirects of the navigation, `cookies` those the context holds for its URLs, `ip_address` the server's address; the browser does not report `http_version`.
- Chrome's network errors (`net::ERR_*`) map onto `Timeout`, `ConnectError`, `ProxyError`, `TLSError`, and `TooManyRedirects`, and others onto `FetchError`, with `code` 0 and the error's name in the message.

**Capacity**

- Three levels of limits: `max_contexts`, `max_pages_per_context`, `max_pages`. Browser concurrency slots are counted separately from HTTP concurrency slots.
- Browsers are replaced by a new generation, triggered by age, cumulative page count, or memory: in-flight pages are allowed to finish first, then a new browser takes over.

**Failures**

| Failure | Handling |
| --- | --- |
| Page crash | Close the page; the Request is handled as a retryable error |
| Browser disconnected | The current generation is marked invalid, in-flight Requests end with `BrowserLost` (not counted against the normal retry count), and the browser is restarted under a single-flight lock |
| Driver process dies | No notification is emitted in this case: the error type is recognized at the boundary and periodic health probes are run, then patchright is restarted entirely |

**Defaults**

- `headless=True` (`browser.headless`), using the new headless mode; the UA is not overridden.
- `browser.launch_options` are passed to patchright's `launch()` as they are, over the defaults, for control the settings do not give, such as `executable_path` or `args`.
- A `stealth` preset is also provided: system Chrome, headed mode, no viewport set.
- Resource blocking is off by default.

### 5.3 Sessions and Cookie interoperability

- **What a Session contains**: a Session holds Cookies and browser storage state, a bound proxy, a Profile, and an optional reference to an Account. The HTTP side and the browser side are both just caches of the same Session state.
  - **HTTP side**: Cookies are written before a Request is sent, and changes are merged after the Response arrives. curl-cffi's Cookie format loses attributes such as SameSite, so the Fetcher applies the `Set-Cookie` headers to the Session itself, keeping that metadata (§5.1).
  - **Browser side**: storage state is injected when a context is created, and is exported and merged back into the Session before the context closes.
- **Types** (`pernix.Session`, `pernix.CookieJar`, `pernix.Cookie`):
  - A `Session` has an `id` (random unless given), its `cookies` (a `CookieJar`), `local_storage` by origin, and an optional `proxy`, `profile`, and `account`. `Session.state()` and `Session.from_state(id, state)` convert it to and from a `SessionState`, a msgspec Struct, as are the `Cookie` records, so state is stored without pickle (ADR 0004).
  - A `Cookie` is a full record: name, value, domain (lower-cased and in ASCII), host-only flag, path, expiry, Secure, HttpOnly, SameSite, and the times it was created and last modified.
  - The `repr` of each shows neither cookie values nor storage, and a proxy without its credentials.
- **Cookie jar**: `CookieJar.receive(url, set_cookie_headers)` stores cookies and `matching(url)` gives those a browser would send, as RFC 6265bis and Chrome do:
  - Cookies are kept by name, domain, host-only flag, and path; one that replaces another keeps its creation time.
  - A `Domain` must be the host or a domain above it, and not a public suffix, from the Public Suffix List with its private section, unless it is the host itself, which makes a host-only cookie; IP addresses take host-only cookies only.
  - The path defaults to the directory of the request path, and a cookie goes to its path and the paths under it.
  - Secure cookies come from, and go to, secure origins only: HTTPS and WSS, and loopback hosts. A cookie from an insecure origin cannot replace a Secure one. `SameSite=None` requires Secure, and the `__Secure-` and `__Host-` prefixes are enforced, in any case: `__Host-` cookies are Secure, without a `Domain`, and with an explicit `Path=/`.
  - `Max-Age` takes precedence over `Expires`, expiry is capped at 400 days, and an expired cookie removes the one it would replace. Cookies larger than 4096 bytes, or with control characters other than the tab, are refused.
  - Cookies go longer paths first, then the earliest created first. `SameSite` is kept for the browser but not applied to HTTP fetches, each of which is a navigation of its own; the number of cookies per domain is not limited.
  - Each cookie's modification time lets Session state be merged per cookie, by the Session Pool.
- **Session Pool**: no Session is used by default; Requests are independent (ADR 0011). A Session Pool is opt-in, enabled at the Spider level; once enabled, a Request leases a Session from it by default. This includes Requests created in a Callback: they do not inherit the Session of the page they were found on. `Request(session="xxx")` pins a specific Session; to continue with the current page's Session, pass `session=response.session`. Inline Fetch is the exception: it continues the current Request's Session by default (see §4.6).
  - **Enabling**: `sessions.enabled = true`, usually in `Spider.settings`. The in-process pool (`MemorySessionPool`) belongs to one Run and is named `sessions.pool`. It creates Sessions as Requests need them, when none is free, up to `sessions.size` (8), or all at the start with `sessions.eager`; a Request naming a Session it does not hold yet has it created under that id while there is room, and fails with `SessionError` once the pool is full. Each Session takes `sessions.max_concurrency` (1) fetches at once, and the least recently leased of the free ones is leased first. A Request naming a Session while no pool is enabled fails with `SessionError`.
  - **Leasing a fetch**: a fetch takes its place in its domain slot (§6.3), then leases its Session, then waits for the slot's delay and rate, so it never holds a Session while it waits for a place, which a fetch nested in the one holding the place may need, and holds a place while it waits for a busy Session only for a second at a time, as a fetch nested in the one holding the Session may need the place; the delay, the rate, and a cooldown are checked just before the download, however long the Session took to free up. It holds both until the Response arrives; a streamed Inline Fetch holds them until it is closed. The Request goes down the download chain naming the Session it is sent under, so `response.request.session` and `response.session` name it, and the Fetcher gets the Session as `session=`. The Session is built from the state the pool holds; afterwards, the changes the fetch made are saved as a `StateDelta`, merged per cookie by modification time, removals included, timed as the jar last removed them, whether it held them or not, and the Lease is released with the fetch's outcome. A fetch nested in another of the same Session, in the same task, such as one a download Middleware makes, shares its Lease, and an Inline Fetch made there continues that Session; the Lease is released with the most severe outcome of them all, so a 429 a nested fetch got cools the Session down. Callbacks, errbacks, and Events get the Request as it was sent, naming its Session, so an Inline Fetch in an errback continues it too. Rendered Requests do not lease Sessions until the browser takes them; one naming a Session fails.
  - **Waiting for a Session**: a Request from the Frontier waits a second for a busy Session, then goes back to the Frontier without counting an attempt; it does too while every Session that fits cools down, until the first stops. Inline Fetches wait in place, giving up their place in the slot after each second until the Session frees up. A Request no Session can ever be leased for, as when the one it names is Retired, fails.
- **Health management**:
  - There are three states: available, in Cooldown, and Retired.
  - When Rate Limited (429), a Session only enters Cooldown and is not Retired: until its `Retry-After`, or for `sessions.cooldown` (30 s) without one, at most `sessions.cooldown_max` (300 s). Its domain keeps fetching with the other Sessions (§6.3).
  - Leases carry a token and expire after `sessions.lease_ttl` (300 s) unless renewed, which happens every third of it while their fetch runs; a release or a save from an expired or released Lease is rejected, counted as `sessions.stale`.
  - Stats: `sessions.created`, `sessions.leased`, `sessions.outcomes.<outcome>`, `sessions.cooldowns`, and `sessions.stale`.
  - When judged Blocked, the Session is Retired and the Request is retried with another Session; Session switches have their own budget and do not consume ordinary retries.
- **Login**: an `async def login(self, session)` method. A given Session logs in only once at a time (single-flight), and can log in again automatically when the Session becomes invalid.
- **Proxy sources**: a static list, a file, a refreshable API, or a custom `ProxyProvider`. A proxy's key includes its username and password, so different sticky sessions on the same gateway are not merged into one.
- **Two implementations**: `SessionPool` is a protocol. The in-process implementation is the default and suits a single process; the Redis implementation lets multiple Workers, multiple machines, and even multiple Spiders share the same Session Pool, and uses the same Redis as the distributed Frontier (confirmed).

#### 5.3.1 Session Pool shared across Workers (Redis)

```python
class SessionPool(Protocol):
    name: str
    async def acquire(self, *, session: str | None = None, tags: Collection[str] = (),
                      timeout: float | None = None) -> SessionLease | None: ...
    async def ready_in(self, *, session: str | None = None, tags: Collection[str] = ()) -> float: ...
    async def renew(self, lease: SessionLease) -> bool: ...
    async def release(self, lease: SessionLease, outcome: Outcome) -> bool: ...
    async def load_state(self, lease: SessionLease) -> SessionState: ...
    async def save_state(self, lease: SessionLease, delta: StateDelta) -> bool: ...

# Outcome: ok / rate_limited(until) / blocked / auth_expired / proxy_failed / error
```

1. **Atomic leasing**: a single Lua script does "pick an available Session (not in Cooldown, not Retired, concurrency not full) → store the holder and expiry time → issue a token" in one step. Holdings are tracked in a sorted set of "holder → expiry time"; after a Worker crashes, its holdings are reclaimed by expiry time, so capacity never shrinks permanently. The Lease is renewed automatically while the Request is being processed.
2. **Concurrency limit across Workers**: each Session has a `max_concurrency` that applies across all Workers. Sessions bound to an Account default to 1, i.e. exclusive; anonymous Sessions can be set higher.
3. **Preventing stale writes**: both `release` and `save_state` carry the token. When an old Lease that has been judged expired writes back, the write is rejected and does not overwrite the new holder's state.
4. **Health, Cooldown, and quotas**:
   - Cooldown expiry, error score, use count, daily quota, and minimum interval between uses are all updated atomically in Lua, with time taken from the Redis server clock.
   - `rate_limited` puts the Session in Cooldown until the time given by `Retry-After` or the target site's rate-limit headers; it is not Retired.
   - `blocked` adds to the error score, and the Session is Retired once the score exceeds a threshold.
   - `auth_expired` marks the Session as needing to log in again.
   - A Cooldown or Retirement detected by one Worker takes effect immediately for all Workers.
5. **Merging Cookies and storage state**: merging happens at the granularity of individual Cookies (domain, path, name), each carrying a modification time; a Worker writes back only the Cookies it changed and never overwrites the whole set. Browser localStorage is merged per origin.
6. **Single-flight login**: the cross-Worker login lock carries a TTL and a token. Only one Worker logs in for a given Session while the other Workers wait for the outcome; if the Worker holding the lock crashes, the lock expires automatically.
7. **Proxies work the same way**: proxies are another kind of resource under the same mechanism, and their health is shared across Workers. A proxy's key includes its username and password.
8. **Namespaces and sharing scope** (confirmed): pools have their own names and are not tied to a `run_id`, so different Spiders and different Runs can share the same Session Pool of Accounts or the same proxy pool; concurrency limits, Cooldowns, and quotas apply to all consumers together. All keys share one hash tag, for compatibility with Redis Cluster.
9. **Security**:
   - Cookies, Tokens, and Account credentials are sensitive data; serialization uses msgspec, not pickle.
   - They are automatically redacted in logs and stats.
   - Optional encryption (confirmed): when a key is configured, data is stored encrypted with AES-GCM; otherwise it is stored in plaintext and a warning is issued at startup.
10. **Management boundary**: the framework provides a minimal management API and CLI (`pernix pool add / list / retire / reset`) for importing Accounts or proxies and for troubleshooting. Visual management, alerting, Account registration, and manual review belong to the Platform.

### 5.4 Basic anti-detection

**Profile consistency**

- HTTP uses a pinned-version Chrome Profile by default (`http.impersonate`, `chrome150`), whose major version is aligned with that of the bundled browser once the browser Fetcher lands. Aliases such as `chrome`, which drift as the library is upgraded, are rejected, as are unknown targets, with a suggestion.
- Contract tests guarantee that, for the same Session, the UA major version, `sec-ch-ua`, and OS platform are consistent between the HTTP side and the browser side.
- By default no extra browser characteristics are injected or randomly generated, to avoid contradicting the real TLS/HTTP2 characteristics. Locale-related headers such as `Accept-Language` are derived uniformly from the `locale` setting.

**Pacing**

- Concurrency and delay (with jitter) are set per domain, `Retry-After` is honored, and a `429`/`503` with `Retry-After` makes the whole domain back off together (§6.3).
- Concurrency slots are computed along two dimensions, domain and Session: a fetch takes its domain's slot, whose concurrency, delay, rate, and cooldown apply to all Sessions together, and a Lease on its Session, within the Session's `max_concurrency`; a 429 cools only its Session down (§5.3).

**Handling Blocked responses**

- Blocked-response detection: built-in status-code rules, plus an overridable `Spider.is_blocked(response)`.
- Once a Response is identified as Blocked: retry with a different Session or proxy; optionally, it can be configured to "automatically upgrade to Render once HTTP is Blocked".

**Browser side**: see the defaults in §5.2. The following must be verified by real-world testing before going live:

- the actual values of the UA and `sec-ch-ua` in headless mode;
- the egress actually in effect for proxies set per context;
- cancellation behavior.

## 6. Scheduling layer

### 6.1 Frontier

Interface semantics:

- `add`: atomically performs "deduplicate + insert".
- `claim`: claims a Request and obtains a Lease with a token.
- `renew`: renews the Lease.
- `ack`: acknowledges completion.
- `nack(delay)`: returns the Request with a delay, optionally counting it as a failure.
- `dead`: marks the Request as permanently failed; it is kept as a Dead Request.

Rules:

- All write operations are conditional on `(id, token)`; writes from a stale old Lease have no effect.
- Lease deadlines are taken from the backend clock.
- A Lease carries the item id, a token that increases with every claim, the attempt number (from 1; `nack(count_attempt=False)` does not count, an expired Lease does), the expiry time, the concurrency slot (the host by default, used by `claim(skip_slots=...)`), and the Request.
- `claim` hands out higher priorities first and, within a priority, the Request that became ready first; a Request returned with `nack` queues behind the ready ones. A Request whose Lease expires becomes ready again.
- `wait()` returns once something may have changed (an add, a settled Lease, a passed delay, an expired Lease), so idle Workers do not poll; distributed backends may return after a polling interval.
- Every backend passes the conformance suite in `pernix.testing.frontier`.

Engine integration:

- Each worker claims one Request at a time and renews its Lease every third of `frontier.lease_ttl` (60 s by default) while it works.
- The Lease is acknowledged only after the Callback's outputs have been added, so the Requests a Callback adds are in the Frontier before their parent leaves it, and the Run can never end early.
- A Request that was fetched and handled, or ignored with `IgnoreRequest`, is acknowledged; one whose fetch or Callback failed is kept as a Dead Request with the error as the reason. Retries return Requests with `nack` (§6.4).
- A Finite Run is idle once `start()` is exhausted and nothing is ready, delayed, or leased. Then `SpiderIdle` is emitted and `Spider.on_idle()` runs; the Run ends when it adds no new Request to the Frontier, including when everything it yields is a duplicate or vetoed.

| Implementation | Role |
| --- | --- |
| In-memory | Default; runs the same state machine |
| SQLite | `job_dir` persistence, for resuming crawls on a single machine |
| Redis | Distributed, the preferred implementation (confirmed): ZSET + Lua, with Leases and tokens; the same Redis also handles shared rate limiting and control commands |
| PostgreSQL | Distributed, implemented later as needed, reusing the same protocol and conformance test suite |

**Delivery semantics** are at-least-once, and this is stated explicitly in the documentation.

### 6.2 Deduplication and Fingerprints

- Deduplication is handled by the Frontier, in the same atomic operation as insertion.
- Default Fingerprint: `version | method | normalized URL | body digest | explicitly declared headers`, hashed to 32 hexadecimal characters (`pernix.fingerprint.request_fingerprint`). Headers are declared with `fingerprint.headers`.
  - It never includes cookies, whether passed as `cookies=` or as a `Cookie` header; declaring the `Cookie` header for the Fingerprint is rejected. By default it does not include the fetcher or the Session either, so the same URL is not fetched again just because it was switched to Render or carries different cookies; fetching it again is the application's explicit decision, through `dont_filter` or a Fingerprint it supplies.
  - The Fingerprint function can be replaced with `fingerprint.function`, a callable `(request) -> str` that may fall back to `request_fingerprint()`, or the application can pass in a Fingerprint directly with `Request(fingerprint=...)`.
  - `dont_filter` bypasses deduplication: such a Request is neither checked nor recorded.
- URL normalization does only three things: serialize according to WHATWG rules, remove the fragment, and stably sort query parameters by name (`fingerprint.sort_query`, on by default).
  - It does not change path case, remove trailing slashes, or remove tracking parameters; these can be enabled with optional rules when needed.
  - Percent-encoded bytes are preserved as is.

### 6.3 Concurrency, rate limiting, and robots

- **Concurrency**:
  - Global default 16 (`concurrency`, the number of workers), per-domain default 8 (`domain.concurrency`); browser pages are counted separately.
  - A domain is a host name, and each has its own slot (the Lease's `slot`). A fetch holds its slot from just before the download chain until the Response arrives; Callbacks hold none, but their Inline Fetches hold slots as fetches do, a streamed one until it is closed; a fetch nested in another for the same domain, in the same task, shares its place in the slot, though it is still paced and still waits out a Cooldown (§4.6).
  - Workers claim only Requests whose slot can start a fetch now: `claim(skip_slots=...)` skips slots that are full, cooling down, or waiting for their delay or rate, so one slow domain never holds back the others. Idle workers wake when the Frontier changes or a slot frees up.
  - Requests that are backing off go back to the Frontier to wait and do not occupy concurrency slots.
- **Delay**: `domain.delay` seconds between the starts of two fetches from one domain, randomized within ± `domain.delay_jitter` (50 % by default).
- **Rate limiting**: the GCRA algorithm on a monotonic clock, `domain.rate` fetches per second with bursts of `domain.burst`, off by default. In distributed mode a shared rate-limiting backend is used instead, and permit times follow the backend clock.
- **Cooldown**: a `429` or `503` with `Retry-After` pauses its whole domain for that long, up to `domain.cooldown_max` (300 s), while other domains continue; with a Session Pool, a `429` cools its Session down instead, and only a `503` pauses the domain (§5.3); the Request itself is retried under the retry policy (§6.4). A Request claimed for a domain that starts cooling down before its fetch goes back to the Frontier until the cooldown ends, without counting an attempt.
- **Offsite**: `Spider.allowed_domains` (subdomains included) admits only Requests to those hosts. The built-in `OffsiteFilter` Extension, enabled by default, vetoes the others at admission, emitting `RequestDropped` with the reason `offsite`; `dont_filter` does not bypass it.
- **robots.txt** (not obeyed by default, confirmed; enable with `robots.obey = true`). Behavior when enabled:
  - Cached per origin for 24 hours; concurrent Requests to the same origin fetch robots.txt only once.
  - Status code handling: 4xx means everything is allowed; 5xx or a network error means everything is disallowed, cached briefly and then retried.
  - The larger of `Crawl-delay` and the configured delay is used.

### 6.4 Retries

After every attempt that produced a Response, raised while fetching, or raised `Retry` in the Callback, the Engine asks the retry policy, `decide(request, outcome, attempt) -> Decision`, where `outcome` is the Response or the exception and `attempt` counts from 1. The Decision is one of:

- `accept()`: the outcome stands; a Response goes to the spider chain, an exception fails the Request.
- `retry(delay, reason)`: the Request is fetched again after the delay, and `RequestRetried` is emitted.
- `give_up(reason)`: a retry was called for but none is left; the Request fails with `RetriesExhausted`.

Default policy (`DefaultRetryPolicy`, configured by `retry.*`):

- At most 3 retries (`retry.times`; 0 turns retrying off).
- Retried status codes (`retry.statuses`): 408, 429, 500, 502, 503, 504, 522, 524. Retried exceptions: `Timeout`, `ConnectError`, `ProxyError`, and `Retry`; other `FetchError`s, such as `TLSError` or `TooManyRedirects`, would fail again.
- The delay is the one `Retry` names; otherwise a `Retry-After` header's, in either format, up to 300 s (`retry.retry_after_max`); otherwise full-jitter backoff with a 1 s base (`retry.backoff_base`) doubling per retry, up to 60 s (`retry.backoff_max`). `retry.delay_scale` multiplies every delay; tests set it to 0.
- `retry.policy` replaces the policy with any object that has `decide()`; a class gets `RetrySettings` injected. A policy that raises keeps the outcome.

Rules:

- For Requests that go through the Frontier, a retry returns the Request with `nack(delay)`, so it holds no concurrency slot while it waits, and every Middleware runs again; Inline Fetches are retried in place.
- A Request whose retries ran out, or that failed in a way the policy keeps, is kept as a Dead Request, `RequestFailed` is emitted, and its errback runs.
- `CancelledError` is never caught: when a Run is cancelled, Requests in flight are returned to the Frontier without counting an attempt, never retried.

### 6.5 Resuming and distributed crawling

**Resuming**

- Specifying `--job-dir` (or the `job_dir` setting) enables the SQLite Frontier.
- Running again with the same directory resumes the crawl: pending, delayed, and Lease-expired Requests all continue.
- Persisted state is never deleted on shutdown; cleanup is left to the user or the Platform.

**Distributed**

- Multiple processes or machines use the same `run_id` and the same backend, sharing the Frontier, the deduplication set, and the rate-limiting budget.
- Initial Requests are seeded only once.
- Completion check: the numbers of claimable, delayed, and leased Requests are all 0, and this holds for N consecutive checks.
- There are two Run modes: `finite` (a Finite Run, which ends once the Frontier is drained) and `continuous` (a Continuous Run, which never ends on its own and is stopped from outside).

### 6.6 Stopping

| Mode | Behavior |
| --- | --- |
| `drain` | Stop claiming new Requests and finish the in-flight ones within a grace period |
| `force` | Cancel in-flight Requests, return them to the Frontier (not counted as failures), and close components |
| `pause` / `resume` | Stop or resume claiming; the process stays alive (planned, with the Platform's control sources) |

- `crawler.stop(mode="drain")` requests a stop; it returns at once, and can be called from a Callback, a component, or another task, even before the Run starts. A Force Stop requested during a Drain escalates it; any other repeated request, and any request after the Run, does nothing.
- **Drain**: the workers stop claiming, and the iteration of `start()` stops; `on_idle()` is not called. The Requests in flight are finished, and the Requests their Callbacks yield still enter the Frontier, where a later Run can resume them. The queued Items are processed. All of it must end within `shutdown.grace` (20 s); then the Drain turns into a Force Stop. The Run ends as `drained`.
- **Force Stop**: the Requests in flight are cancelled and returned with `nack(count_attempt=False)`. The Item processing is cancelled too: the Items queued or in process are discarded, counted as `items.discarded`, and logged as a warning. The components are then closed as usual, but each within `shutdown.close_timeout` (5 s), so a stuck close cannot hold the process; a sync `close()` cannot be cancelled. The Feeds are completed, keeping the Items written. The Run ends as `forced`. A cancelled Run is closed the same way.
- Stopping covers the whole Run, from opening the components to closing them. A Drain requested while the components open applies once the work starts, and its grace period runs from the request. A Force Stop while they open cancels the opening and closes the components already open; the Run ends as `forced` without `SpiderOpened` or `SpiderClosed`, as when opening fails. A Force Stop while they close, including one a Drain turns into after its grace period, limits the close in progress and the rest to `shutdown.close_timeout`.
- Requests from `start()` and `on_idle()` are checked against the Drain before each one, since adding to the Frontier need not suspend.
- Signals, in `run()` and the CLI: the first SIGINT/SIGTERM triggers a Drain, and the second triggers a Force Stop. Any further one gets Python's default handling (`KeyboardInterrupt`, or termination), an escape hatch when sync code blocks the event loop. The defaults of 20 s and 5 s together stay below the usual 30 s a container platform waits before killing a process.
- Each stage of shutdown is fault-tolerant on its own, and multiple exceptions are aggregated into an ExceptionGroup and reported together; a close that times out is one of them.

## 7. Built-in components

| Component | Default | Replaceable / can be disabled |
| --- | --- | --- |
| HTTP Fetcher (curl-cffi) | Enabled | ✓ |
| Browser Fetcher (patchright) | Available after installing `pernix[browser]` | ✓ |
| Deduplication / Fingerprint | Enabled | ✓ |
| Offsite filtering (`allowed_domains`) | Enabled when set | ✓ |
| Retries | Enabled | ✓ |
| Rate limiting / concurrency slots | Enabled | ✓ |
| robots.txt | Disabled (enable with `robots.obey`) | ✓ |
| Session Pool | Opt-in; Requests are independent by default (ADR 0011) | ✓ |
| Proxies | Enabled when configured | ✓ |
| Blocked-response detection | Enabled (status-code rules) | ✓ |
| Stats / event loop lag sampling | Enabled | ✓ |
| Feeds (jsonl/csv/json) | Enabled with `-o` or settings | ✓ |
| Development response cache | Disabled | ✓ |
| Link extractor, Sitemap | Utility functions | — |
| API crawling helpers (paginator, Token refresh) | Utility classes, provided in M4 | — |
| Database Pipelines (MySQL / PostgreSQL / MongoDB / Kafka) | Optional extra | ✓ |
| Prometheus / OpenTelemetry | Optional extra | ✓ |

## 8. Observability and the Platform interface

**Run identification**: every Run has a `run_id`: the `run_id` setting, which a Platform passes for example as `PERNIX_RUN_ID`, or else a generated one (32 hexadecimal digits). It is `crawler.run_id`; log records carry it with the `spider` name, and stats as `run.id` and `run.spider`.

**Logging**

- Uses the standard library logging `pernix.*` logger hierarchy and does not modify the root logger. `Spider.logger` is `pernix.spiders.<name>`.
- During a Run, a bounded queue handler on the `pernix` logger takes each record without blocking, and a writer thread delivers it, so a slow or blocked output never stalls the event loop. When the queue is full (`log.queue_size`, 10 000 records), records are dropped and counted. When the Run ends, Pernix waits up to 5 s for the queued records. Meanwhile the `pernix` logger does not propagate, so no record is delivered twice; it is restored afterwards.
- Before a record is queued, its message is merged with its arguments, its exception is rendered as text (`exc_text`), and its context is attached from contextvars: `run_id`, `spider`, and `request_id`, the Frontier's id of the Request being processed, which stays the same across retries and is `None` outside a Request.
- Output (`log.output`): by default, if the application configured logging (the root logger has handlers), records go to the root logger's handlers, still from the writer thread; otherwise Pernix writes them to stderr itself. `"stderr"`, `"stdout"`, and `"root"` force a choice. Logs go to stderr by default so that stdout stays free for data.
- Format (`log.format`): human-readable text on a terminal, and JSON lines otherwise, or as configured. A JSON line holds `time` (UTC), `level`, `logger`, `message`, `run_id`, `spider`, `request_id`, `exception` and `stack` when present, and the record's extra fields. `log.level` (INFO) applies when Pernix writes the output; the root handlers use the application's levels.
- Redaction, in the writer thread, of messages, exception texts, and the strings in extra fields, such as the stats of a snapshot (copies are masked; the originals are unchanged): credentials in URLs (`http://user:pass@proxy` becomes `http://***@proxy`) and in proxies without a scheme, where only a user and a password joined by a colon count, so that e-mail addresses are kept (`user:pass@proxy` becomes `***@proxy`); the values of sensitive names as query parameters, form fields, quoted pairs, and header lines, such as `Cookie`, `Set-Cookie`, `Authorization`, `Proxy-Authorization`, `token`, `access_token`, `api_key`, `password`, `secret`, `signature`, and `sessionid`, plus the names in `log.redact`; and the values of `Secret` settings wherever they occur.

**Stats**

- `crawler.stats` (also `Spider.stats`, and injected into components annotated with `Stats`) holds the Run's stats in memory: `inc(key, count=1)`, `set(key, value)`, `max(key, value)`, `get(key, default=None)`, and `snapshot()`. Values are ints, floats, or strings. The stats stay readable after the Run.
- Keys are dot-separated; Pernix names its segments in snake_case, keeps the spelling of labels such as status codes, reasons, and error types, and never makes one of its keys the prefix of another, so a snapshot can be nested by its dots. Breakdowns use a `_by_<dimension>` segment. Pernix reserves the prefixes below; Spiders and components use their own.

| Key | Meaning |
| --- | --- |
| `run.id`, `run.spider`, `run.started_at`, `run.finished_at`, `run.elapsed`, `run.reason` | The Run; times in UTC ISO 8601, `elapsed` in seconds, `reason` as in `SpiderClosed` |
| `requests.enqueued` | Requests that entered the Frontier |
| `requests.dropped`, `requests.dropped_by_reason.<reason>` | Requests vetoed or dropped as duplicates, such as `duplicate` and `offsite` |
| `requests.retried`, `requests.ignored` | Attempts retried; Requests ignored |
| `requests.failed`, `requests.failed_by_error.<ErrorType>` | Requests that failed for good |
| `downloads.responses`, `downloads.by_status.<code>`, `downloads.bytes` | Responses fetched, including those retried afterwards and those of Inline Fetches, and their body bytes, streamed ones as they are read |
| `downloads.errors`, `downloads.errors_by_type.<ErrorType>` | Exceptions fetching raised, other than `IgnoreRequest` and `Retry` |
| `responses.received` | Responses handed to Callbacks |
| `items.scraped`, `items.dropped`, `items.errors`, `items.retried`, `items.failures_lost`, `items.discarded` | Items stored, dropped, or failed; Pipeline calls retried; failed Items a failure Pipeline lost; Items a stopped Run never processed |
| `frontier.ready`, `frontier.delayed`, `frontier.leased`, `frontier.done`, `frontier.dead` | The Frontier at the snapshot |
| `throttle.waits`, `throttle.wait_seconds`, `throttle.cooldowns`, `throttle.refused` | Waiting for per-domain slots, and cooldowns |
| `loop.lag_avg`, `loop.lag_max` | Event loop lag in seconds, sampled every second |
| `log.warning`, `log.error`, `log.critical`, `log.dropped` | Records logged per level, and records dropped from the full queue |

- Every `stats.interval` (60 s; 0 turns it off), a `StatsSnapshot` Event is emitted and an INFO line logged, such as "Fetched 120 responses (60/min), scraped 340 items (170/min)", with the stats in its `stats` field. After `SpiderClosed` and before the components are closed, the final snapshot (`final=True`) is emitted and logged as the Run's summary; text output lists every stat.
- **Stats Sinks** (`stats.sinks`, as `{sink: order}`) report snapshots elsewhere, such as to the Platform. A sink has `push(snapshot)`, sync or async, which is subscribed to `StatsSnapshot`, so a sink that fails or times out does not affect the Run. Sinks are built and opened like Extensions, before them, and receive the final snapshot before they are closed.

**Control**

- Control through OS signals is supported (§6.6); within a program, `crawler.stop(mode)` can be called, and `pause()` and `resume()` are planned.
- Pluggable `ControlSource`: for example, reading pause/drain/stop commands from a Redis key. The framework only reads the control state and responds to it; writing it is done by the Platform.

**Outcome**

- The process exit code distinguishes the following cases (§4.12): completed normally (0), error (1), configuration error (2), interrupted by a signal (130).
- A final outcome file can optionally be written.

## 9. Testing support (`pernix.testing`)

```python
from pernix.testing import FakeFetcher, crawl, make_response, run_callback

async def test_books():
    fake = FakeFetcher({
        "https://shop.example/": "<a href='/book/1'>Dune</a>",
        re.compile(r"https://shop\.example/book/\d+"): "<h1>Dune</h1>",
        "https://shop.example/flaky": [Timeout("slow"), "<p>ok</p>"],   # answers in turn, the last repeating
    })
    report = await crawl(Books, fetcher=fake, keyword="dune")          # Spider arguments as keywords
    assert report.items == [{"title": "Dune"}]
    assert report.stats["items.scraped"] == 1
    assert fake.unmatched == []

async def test_parse():
    outputs = await run_callback(Books(), make_response("https://shop.example/", "<a href='/book/1'>Dune</a>"))
    assert [request.url for request in outputs] == ["https://shop.example/book/1"]
```

- **Fake fetching**: `FakeFetcher(routes)` answers Requests from canned answers. A route's key is a URL, matched exactly, or a compiled pattern the whole URL must match; the first route added that matches answers, and `add(key, answer, method=...)` can limit one to a method. An answer is a body (200), a `Response` (bound to the Request fetched, so its Callback runs), an exception to raise, or a function of the Request returning or awaiting one of those; a list of answers is used in turn, its last one repeating. Unmatched Requests get 404 and are recorded in `unmatched`, every Request in `requests`. It fetches `http` and `https`, standing in for the HTTP Fetcher when registered as `http`.
- **Responses**: `make_response(url, body, status=, headers=, json=, request=, encoding=)` builds a Response as a Fetcher would: a `str` body is UTF-8 HTML unless the headers say otherwise, `json=` makes a JSON body, and `request` (by default `Request(url)`) carries the Callback, `kwargs`, and `meta`.
- **Running**: `crawl(Spider, fetcher=..., settings=..., **args)` runs a Spider to completion and returns a `CrawlReport` with the Items that passed every Pipeline, the final stats, the reason, and the `run_id`. It runs without periodic stats, retry delays, or per-domain delay, rate, and cooldowns, as those only make a test wait; `settings` override that. `run_callback(spider, response)` runs one Callback as a Run would, the Request's or `parse`, with its `kwargs`, resolving relative URLs against the Response, and returns its outputs; spider Middleware do not run, and Inline Fetches need `crawl()`.
- **Local server**: `LoopbackServer` serves on 127.0.0.1: routes registered with `route()` or given up front (a body, a `Reply`, or a handler of the `ReceivedRequest`), then the files of a `directory`. It records every request with its headers in wire order.
- **Network guard**: while `blocking_network()` is in effect, the Fetchers reach only loopback hosts (loopback addresses, `localhost`, `*.localhost`), or any host through a proxy on one; others raise `NetworkBlocked`, a `FetchError`, and are recorded in the guard. A host is read as the HTTP Fetcher gives it to curl (`pernix.urls.url_host()`), so `localhost` in full-width letters, or as `local%68ost`, is a loopback host too. The HTTP Fetcher checks the URL before connecting, and each redirect's `Location` before curl follows it, as urllib and as WHATWG read it, since the two can name different hosts; a `Location` that cannot be parsed is refused. A browser launched while the guard is on sends everything but loopback traffic to a proxy that refuses it, set at launch so that the browser's own requests, which belong to no page, are stopped too; a Request's loopback proxy takes its context's requests instead. The requests its pages make to other hosts, redirects and WebSockets included, are recorded, the browser's own are not, and a refused document raises `NetworkBlocked`. A browser launched while the network was open refuses to render under the guard. This layer is necessary: curl-cffi and the browser do not go through Python's socket, so the usual network-blocking plugins cannot stop them.
- **pytest plugin**, loaded as `pernix` once Pernix is installed (`-p no:pernix` disables it): the network guard is on while each test is set up, run, and torn down, fixtures of every scope included, and the phase during which it refused a URL fails, even when the refusal happened inside a Run, which only logs a failed fetch. `@pytest.mark.pernix_network` lets a test use the network, and the ini option `pernix_network = allow` lets every test. The `loopback_server` fixture gives a started `LoopbackServer`.
- `pernix.testing` itself does not need pytest; the plugin and the Frontier conformance suite (`pernix.testing.frontier`) do.
- **Capture and replay** (planned): unified for HTTP and the browser, with capture, replay, and mixed modes.
- **The framework's own tests**: use the same tools; network failures are simulated with scripted loopback servers, and later a fault-injection proxy; cancellation semantics are regression-tested across a matrix of Python versions.

## 10. Runtime and dependencies

- **Python**: 3.13 minimum, 3.14 recommended.
  - 3.13 fixes TaskGroup losing a cancellation in the "external cancellation + child task failure" case, and provides `Queue.shutdown`; Drain depends on both.
  - New 3.14 capabilities are enabled only after a version check.
- **Concurrency model**: native asyncio, with no dependency on AnyIO. One process runs only one event loop.
  - Multiple processes are handled by an optional launcher or by the Platform.
  - uvloop is an optional extra.
- **Core dependencies**: curl-cffi, parsel (lxml), msgspec, orjson, protego, charset-normalizer, and a WHATWG URL parsing library.
- **Optional extras**: `browser` (patchright, pinned), `redis`, `postgres`, `mysql`, `mongo`, `kafka`, `prometheus`, `otel`, `selectolax`.
  - The core package imports fully without any extra installed.
  - Using an extra that is not installed produces a clear installation hint.

## 11. Milestones (proposed)

| Phase | Contents |
| --- | --- |
| M0 Kernel | Settings, Events, Request/Response, Engine, in-memory Frontier, HTTP Fetcher, deduplication, retries, rate limiting, stats, logging, Feeds, `run`/`fetch`/`shell`/`check`, `pernix.testing` basics |
| M1 Browser and Sessions | Browser Fetcher, `render`, Session/Session Pool, Cookie interoperability, proxies, Blocked-response detection, anti-detection defaults, `pernix fetch` for agents (text and Markdown extraction, selection, size limits, batches, a private-address guard) |
| M2 Persistence and control | SQLite Frontier, `job_dir` resuming, stop modes, `ControlSource`, robots |
| M3 Distributed | Redis Frontier, Redis Session Pool, shared rate limiting, completion check, Redis `ControlSource`, `pernix pool` commands |
| M4 Ecosystem | API crawling helpers, file downloads, Sitemap, database Pipelines (MySQL / PostgreSQL / MongoDB / Kafka), Prometheus/OTel, capture and replay |
