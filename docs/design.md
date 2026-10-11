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
    Slot --> DMW[Download Middleware onion\nrobots / Session / retry / custom]
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
        # Run arguments: both CLI -a keyword=xx and Platform-supplied arguments are converted and validated by type annotation
        self.keyword = keyword
        self.max_pages = max_pages

    async def start(self):               # optional; falls back to start_urls if omitted
        yield Request(f"https://api.example.com/s?q={self.keyword}", callback=self.parse_list)

    async def on_start(self): ...        # optional: before crawling starts (e.g. to warm up a login)
    async def on_idle(self): ...         # optional: can still yield new Requests when the Frontier is idle
    async def on_close(self, reason): ...  # optional: on close
```

- One Crawler corresponds to one Spider object. Settings live on the Crawler object and no module-level global variable is modified, so one process can run multiple Spiders in parallel.
- `self.logger`, `self.crawler`, and `self.stats` are available at runtime.

### 4.3 Request

```python
Request(
    url,
    method="GET",
    headers=None,          # order-preserving, case-preserving, duplicate keys allowed
    body=None, json=None, data=None,
    callback=None,         # defaults to parse; must be a Spider method (referenced by name when persisted)
    errback=None,
    kwargs=None,           # keyword arguments passed through to the Callback
    meta=None,             # user data; must be JSON-serializable
    priority=0,            # higher goes first; FIFO within the same priority
    dont_filter=False,     # skip deduplication
    fingerprint=None,      # application-supplied Fingerprint; computed by default, see §6.2
    session=None,          # pin a Session; assigned by the Session Pool by default, see §5.3
    render=None,           # browser Render options, see §4.7
    fetcher=None,          # explicit Fetcher; inferred from render and the URL scheme by default
    timeout=None,
    proxy=None, impersonate=None,   # per-Request overrides, rarely needed
)
```

- Requests are immutable; derive new ones with `request.replace(...)`.
- Relative URLs: a Request created while a Callback is running resolves a relative `url` against that Callback's Response, using its final URL after redirects and honoring `<base href>` in HTML. `request.url` is therefore absolute as soon as the Request is constructed; this also applies to Inline Fetch. Outside a Callback (for example in `start()` or `on_idle()`), `url` must be absolute, and a relative URL raises an error.
- `meta` holds only user data. Framework-internal state (retry count, the Session used, etc.) lives in typed fields, and component-private data lives in namespaces keyed by component name, so different components do not become implicitly coupled through `meta`.
- Serialization uses msgspec with a schema version. Callbacks are referenced by method name; if the method cannot be found on resume, an error is raised immediately, and it never silently falls back to `parse`.

### 4.4 Response

- Common fields: `url` (final URL), `status`, `headers` (multi-valued), `body: bytes`, `request`, `meta`, `session`, `cookies`, `history` (redirect chain), `elapsed`, `ip_address`, `http_version`, `fetcher`.
- `text`: decoded lazily, in this order: user-specified → BOM → HTTP charset → `<meta>` / XML declaration → strict UTF-8 → encoding detection → configurable default. Encoding labels are mapped following browser rules; for example, both `gb2312` and `gbk` are decoded as `gb18030`. The encoding actually used and its source are stored in `encoding` / `encoding_source`.
- Selectors: `css()`, `xpath()`, `re()`, `jmespath()`; the document is parsed only on first call. `json()` decodes with orjson and caches the decoded value.
- Links: `urljoin()` resolves a URL against the Response's base URL (final URL, honoring `<base href>`), for example to store absolute links in Items. There are no `follow` helpers: Requests are always written out explicitly, and relative URLs are resolved automatically (see §4.3).
- Responses from the browser also carry `response.browser`: captured XHRs, screenshots, and action outputs; with `keep_page` enabled there is also `response.page`, see §4.7.

### 4.5 Callbacks and output semantics

- A Callback can be an `async def` async generator that yields `Request` objects or Items (None is ignored), or a plain `async def` that returns a single object, a list, or None.
- **Output is streamed**: each yielded object is processed immediately. Requests go to the Frontier; Items go into a bounded queue, and when the queue is full the Callback is suspended (backpressure).
- **When a Callback raises** (confirmed):
  - The Request is marked as failed, the errback is called, and the failure is counted in stats. It is **not retried by default**, because parsing errors are usually deterministic.
  - Output yielded before the exception is kept.
  - To retry, the user explicitly writes `raise Retry(reason)`; deciding whether a page is Blocked is covered in §5.4 and handled automatically by the framework.
- Every Request must go through the Frontier; a Callback cannot bypass scheduling and hand a Request directly to a Fetcher. For cases that need the Response "right away", use the Inline Fetch in §4.6.

### 4.6 Inline Fetch (`await self.fetch`)

```python
async def parse_detail(self, response):
    api = await self.fetch(Request(comments_api(response)))   # also accepts a URL string
    yield {"title": response.css("h1::text").get(), "comments": api.json()["data"]}
```

- Use cases: logging in, exchanging for a Token, paging sequentially through a paginated API, and cases such as "detail page + comments API" where the output has to be assembled within one Callback.
- Semantics:
  - Goes through the same download Middleware chain and the same rate limiter, and uses the current Request's Session by default.
  - **Does not enter the Frontier, is not deduplicated, and is not persisted**; on failure it is retried in place according to the retry policy, and `FetchError` is raised if it ultimately fails.
  - When the parent Request is cancelled, its Inline Fetches are cancelled with it.

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

- The framework **does not build its own Item/Field system**. dict, dataclass, pydantic, msgspec, and attrs objects can all be Items; an adapter layer converts them to dicts uniformly before export. For validation, use pydantic or msgspec models directly.

```python
from pernix import Pipeline, DropItem

class Clean(Pipeline):
    async def process(self, item, ctx):
        if not item["title"]:
            raise DropItem("empty title")      # returning None also drops the Item
        return item

class SaveMySQL(Pipeline):
    accepts = (Book,)                          # optional: only handle Items of these types
    batch_size = 500                           # optional: enable batch mode
    async def open(self, crawler): ...
    async def process_batch(self, items, ctx): ...
    async def close(self): ...
```

- Pipelines run in order. `open` / `close` are managed by the framework: if startup fails, the components already opened are closed in reverse order.
- File output (Feeds) is built in: `-o books.jsonl` or `settings["feeds"]`, supporting jsonl, csv, and json.
- Four writer Pipelines are built in, for MySQL, PostgreSQL, MongoDB, and Kafka, and are installed as optional extras (confirmed); the database ones support batch writes and upserts by business key.
- Delivery semantics are at-least-once: output may be produced again after crash recovery. The framework provides helpers for upserting by business key and does not promise exactly-once.

### 4.9 Middleware

Download Middleware uses the onion model:

```python
from pernix import Middleware, IgnoreRequest

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

- On a single chain, the order in the request direction and in the response direction is naturally symmetric, and exceptions propagate naturally along the chain; there is no asymmetry such as "exceptions raised in the response phase bypass exception handling".
- Control flow is expressed with exceptions such as `IgnoreRequest`, `Retry`, and `DropItem`; the framework emits the corresponding Events uniformly (`request_ignored`, `retry_exhausted`, etc.).
- A `NodeMiddleware` adapter is also provided, which wraps `process_request` / `process_response` / `process_exception`-style classes into the onion to make migrating existing code easier.

### 4.10 Events and Extensions

```python
from pernix import Extension, events

class Notify(Extension):
    @events.on(events.SpiderClosed)
    async def closed(self, event): ...

crawler.events.on(events.ItemScraped, handler)   # can also be registered dynamically
```

Events are typed dataclasses, and each Event declares how it is executed:

| Form | Execution | Use case |
| --- | --- | --- |
| `observe` | Awaited in order; if a subscriber raises, the exception is logged and isolated without affecting the other subscribers | Most notification Events |
| `gate` | A veto from any subscriber short-circuits; each Event declares individually whether it is fail-open or fail-closed | For example, filtering before admission to the Frontier |
| `transform` | Applied in sequence, with each subscriber's output passed to the next | Events whose data is rewritten layer by layer |

Other conventions:

- Both sync and async subscribers are supported, and coroutines are never silently dropped. Each subscriber has a timeout, and shutdown Events run their subscribers in reverse order of registration.
- **Addons** bundle "settings changes + component registrations" together and declare the range of API versions they are compatible with. They can be discovered through entry points, but are loaded only when explicitly enabled.

### 4.11 Settings

Layers, from lowest to highest precedence:

1. Built-in defaults
2. Addons
3. Project file (`pernix.toml`, or `[tool.pernix]` in `pyproject.toml`)
4. Environment variables `PERNIX_*`
5. `Spider.settings`
6. `run()` arguments or CLI arguments

Rules:

- Keys are lowercase with dot-separated namespaces, for example `concurrency`, `domain.delay`, `http.impersonate`, `browser.headless`, `retry.times`.
- Settings are merged and type-checked at startup; a misspelled key raises an error that suggests similar key names. After startup the settings are frozen and no longer change.
- Components (Middleware, Pipelines, Extensions) are registered as `{component: order}`, and a value of `None` disables a component, for example `"middlewares": {SignRequest: 500, "pernix.retry": None}`. A component can be given as a class, an object, or an import path.
- Components can declare their own settings section, which takes part in validation; at construction time the framework injects the Crawler and the settings, and there is no global service locator.
- Secret settings use the Secret type and are automatically redacted in logs and stats.

### 4.12 Running and the CLI

```python
Books.run(output="books.jsonl", concurrency=32)       # synchronous entry point, suited to scripts
await Books.crawl(keyword="x")                        # run inside an existing event loop
async for item in Books.stream(keyword="x"):          # bounded streaming consumption, suited to embedding in other applications
    ...
```

| Command | Purpose |
| --- | --- |
| `pernix run spider.py[:Name] -a k=v -s key=val -o out.jsonl --job-dir DIR` | Run a Spider |
| `pernix shell URL [--render]` | Interactive debugging with `response` preloaded |
| `pernix fetch URL [--render] [--impersonate X]` | Fetch once and print the Response |
| `pernix new NAME` | Generate a project skeleton (optional; project mode does not enforce a directory layout) |
| `pernix check` | Validate settings, components, and the browser environment |
| `pernix browser install` | Install browser engines |

## 5. Fetching layer

### 5.1 HTTP Fetcher (curl-cffi)

**curl session pool**

- Underlying curl sessions are grouped and reused by `(Session, Profile, curl session options)`, and evicted by LRU plus an idle timeout. A single underlying curl session never mixes different Profiles.
- impersonate is set explicitly on every Request, so a reused connection handle never carries over the previous Request's Profile.

**Timeouts and cancellation**

- `timeout` maps to curl's connect/read timeouts; the overall deadline (including time spent waiting in line) is enforced by the framework with asyncio.
- curl-cffi exceptions are mapped onto the framework's own exception hierarchy: `Timeout`, `ConnectError`, `ProxyError` (407 responses also fall under this), `TLSError`, `TooManyRedirects`, `DecodeError`; the original curl error code is preserved on the exception.

**Concurrency**

- Per-host connection limits are implemented by the framework's concurrency slots, not by curl's multi-handle options.
- HTTP/2 connection reuse is enabled.

**Data**

- Request headers preserve order and case, and support duplicate keys.
- Response headers are read with all multiple values preserved, and `Content-Encoding` is removed from decompressed responses.
- Streaming downloads: `async with crawler.stream(request)`, with a byte limit, and can be written directly to a file.

**Delegated to the framework**

- Cookies are managed by the framework's Session, see §5.3.
- Retries are handled by the framework; curl-cffi's built-in retries are turned off.

### 5.2 Browser Fetcher (patchright)

**Topology**

- patchright is started once per process; it launches one browser with `launch()` and creates multiple contexts, one per Session.
- A context's proxy, storage state, locale, and time zone are set once at creation and never modified afterward; switching proxies means creating a new context.

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

- `headless=True`, using the new headless mode; the UA is not overridden.
- A `stealth` preset is also provided: system Chrome, headed mode, no viewport set.
- Resource blocking is off by default.

### 5.3 Sessions and Cookie interoperability

- **What a Session contains**: a Session holds Cookies and browser storage state, a bound proxy, a Profile, and an optional reference to an Account. The HTTP side and the browser side are both just caches of the same Session state.
  - **HTTP side**: Cookies are written before a Request is sent, and changes are merged after the Response arrives. curl-cffi's Cookie format loses attributes such as SameSite, so the merge preserves that metadata.
  - **Browser side**: storage state is injected when a context is created, and is exported and merged back into the Session before the context closes.
- **Session Pool**: by default a Request leases a Session from the Session Pool. This includes Requests created in a Callback: they do not inherit the Session of the page they were found on. `Request(session="xxx")` pins a specific Session; to continue with the current page's Session, pass `session=response.session`. Inline Fetch is the exception: it continues the current Request's Session by default (see §4.6).
- **Health management**:
  - There are three states: available, in Cooldown, and Retired.
  - When Rate Limited (429), a Session only enters Cooldown and is not Retired.
  - When judged Blocked, the Session is Retired and the Request is retried with another Session; Session switches have their own budget and do not consume ordinary retries.
- **Login**: an `async def login(self, session)` method. A given Session logs in only once at a time (single-flight), and can log in again automatically when the Session becomes invalid.
- **Proxy sources**: a static list, a file, a refreshable API, or a custom `ProxyProvider`. A proxy's key includes its username and password, so different sticky sessions on the same gateway are not merged into one.
- **Two implementations**: `SessionPool` is a protocol. The in-process implementation is the default and suits a single process; the Redis implementation lets multiple Workers, multiple machines, and even multiple Spiders share the same Session Pool, and uses the same Redis as the distributed Frontier (confirmed).

#### 5.3.1 Session Pool shared across Workers (Redis)

```python
class SessionPool(Protocol):
    async def acquire(self, *, tags: Collection[str] = (), timeout: float | None = None) -> SessionLease: ...
    async def renew(self, lease: SessionLease) -> bool: ...
    async def release(self, lease: SessionLease, outcome: Outcome) -> None: ...
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

- HTTP uses a pinned-version Chrome Profile by default, whose major version is aligned with that of the bundled browser. Aliases such as `chrome`, which drift as the library is upgraded, are not used.
- Contract tests guarantee that, for the same Session, the UA major version, `sec-ch-ua`, and OS platform are consistent between the HTTP side and the browser side.
- By default no extra browser characteristics are injected or randomly generated, to avoid contradicting the real TLS/HTTP2 characteristics. Locale-related headers such as `Accept-Language` are derived uniformly from the `locale` setting.

**Pacing**

- Concurrency and delay (with jitter) are set per domain, `Retry-After` is honored, and on `429`/`503` the whole domain backs off together.
- Concurrency slots are computed along two dimensions: "domain × Session".

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

| Implementation | Role |
| --- | --- |
| In-memory | Default; runs the same state machine |
| SQLite | `job_dir` persistence, for resuming crawls on a single machine |
| Redis | Distributed, the preferred implementation (confirmed): ZSET + Lua, with Leases and tokens; the same Redis also handles shared rate limiting and control commands |
| PostgreSQL | Distributed, implemented later as needed, reusing the same protocol and conformance test suite |

**Delivery semantics** are at-least-once, and this is stated explicitly in the documentation.

### 6.2 Deduplication and Fingerprints

- Deduplication is handled by the Frontier, in the same atomic operation as insertion.
- Default Fingerprint: `version | method | normalized URL | body digest | explicitly declared headers`.
  - By default it does not include the fetcher or the Session, so the same URL is not fetched again just because it was switched to Render.
  - The Fingerprint function can be customized, or the application can pass in a Fingerprint directly.
- URL normalization does only three things: serialize according to WHATWG rules, remove the fragment, and stably sort query parameters (can be turned off).
  - It does not change path case, remove trailing slashes, or remove tracking parameters; these can be enabled with optional rules when needed.
  - Percent-encoded bytes are preserved as is.

### 6.3 Concurrency, rate limiting, and robots

- **Concurrency**:
  - Global default 16, per-domain default 8; browser pages are counted separately.
  - Requests that are backing off go back to the Frontier to wait and do not occupy concurrency slots.
- **Rate limiting**: the GCRA algorithm on a monotonic clock. In distributed mode a shared rate-limiting backend is used instead, and permit times follow the backend clock.
- **robots.txt** (not obeyed by default, confirmed; enable with `robots.obey = true`). Behavior when enabled:
  - Cached per origin for 24 hours; concurrent Requests to the same origin fetch robots.txt only once.
  - Status code handling: 4xx means everything is allowed; 5xx or a network error means everything is disallowed, cached briefly and then retried.
  - The larger of `Crawl-delay` and the configured delay is used.

### 6.4 Retries

Default policy:

- At most 3 retries.
- Retried status codes: 408, 429, 500, 502, 503, 504, 522, 524; connection, timeout, and proxy errors are also retried.
- Backoff uses full jitter, with a 1 s base and a 60 s cap.
- Both formats of `Retry-After` are parsed, with a maximum wait of 300 s.

Rules:

- For Requests that go through the Frontier, retries are implemented by re-adding them with `nack(delay)`; Inline Fetches are retried in place.
- `CancelledError` is never caught.

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
| `pause` / `resume` | Stop or resume claiming; the process stays alive |

- The first SIGINT/SIGTERM triggers a Drain, and the second triggers a Force Stop.
- Each stage of shutdown is fault-tolerant on its own, and multiple exceptions are aggregated into an ExceptionGroup and reported together.

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
| Session / Cookie | Enabled | ✓ |
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

**Run identification**: every Run has a `run_id` (generated automatically or passed in from outside). All logs, Events, and stats carry the `run_id` and `spider`.

**Logging**

- Uses the standard library logging `pernix.*` logger hierarchy and does not modify the root logger.
- Human-readable format in a terminal; outside a terminal, or when explicitly configured, JSON lines are written to stdout for the Platform to collect.
- Log writes go through a bounded queue and do not block the event loop; when the queue is full, messages are dropped and counted.
- Request context (`run_id`, `request_id`) is injected automatically through contextvars.

**Stats**: stats snapshot Events are emitted periodically, and a summary is output at the end. `StatsSink` is pluggable, so the Platform can implement its own reporting.

**Control**

- Control through OS signals is supported; within a program, `crawler.pause()`, `resume()`, and `stop(mode)` can be called.
- Pluggable `ControlSource`: for example, reading pause/drain/stop commands from a Redis key. The framework only reads the control state and responds to it; writing it is done by the Platform.

**Outcome**

- The process exit code distinguishes the following cases: completed normally, error, configuration error, interrupted.
- A final outcome file can optionally be written.

## 9. Testing support (`pernix.testing`)

- **Fake fetching**: `FakeFetcher` and Response factories, so Callbacks and Pipelines can be tested without network access.
- **Capture and replay**: unified for HTTP and the browser, with capture, replay, and mixed modes.
- **Local fixture server**.
- **pytest plugin**: provides `crawler`-related fixtures and adds network-access protection at the Fetcher layer. This layer is necessary: curl-cffi does not go through Python's socket, so the usual network-blocking plugins cannot stop it.
- **The framework's own tests**: use the same tools; network failures are simulated with a fault-injection proxy; cancellation semantics are regression-tested across a matrix of Python versions.

## 10. Runtime and dependencies

- **Python**: 3.13 minimum, 3.14 recommended.
  - 3.13 fixes TaskGroup losing a cancellation in the "external cancellation + child task failure" case, and provides `Queue.shutdown`; Drain depends on both.
  - New 3.14 capabilities are enabled only after a version check.
- **Concurrency model**: native asyncio, with no dependency on AnyIO. One process runs only one event loop.
  - Multiple processes are handled by an optional launcher or by the Platform.
  - uvloop is an optional extra.
- **Core dependencies**: curl-cffi, parsel (lxml), msgspec, orjson, protego, charset-normalizer, and a WHATWG URL parsing library.
- **Optional extras**: `browser` (patchright), `redis`, `postgres`, `mysql`, `mongo`, `kafka`, `prometheus`, `otel`, `selectolax`.
  - The core package imports fully without any extra installed.
  - Using an extra that is not installed produces a clear installation hint.

## 11. Milestones (proposed)

| Phase | Contents |
| --- | --- |
| M0 Kernel | Settings, Events, Request/Response, Engine, in-memory Frontier, HTTP Fetcher, deduplication, retries, rate limiting, stats, logging, Feeds, `run`/`fetch`/`shell`, `pernix.testing` basics |
| M1 Browser and Sessions | Browser Fetcher, `render`, Session/Session Pool, Cookie interoperability, proxies, Blocked-response detection, anti-detection defaults |
| M2 Persistence and control | SQLite Frontier, `job_dir` resuming, stop modes, `ControlSource`, robots |
| M3 Distributed | Redis Frontier, Redis Session Pool, shared rate limiting, completion check, Redis `ControlSource`, `pernix pool` commands |
| M4 Ecosystem | API crawling helpers, file downloads, Sitemap, database Pipelines (MySQL / PostgreSQL / MongoDB / Kafka), Prometheus/OTel, capture and replay |
