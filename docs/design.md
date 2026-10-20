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
2. An Engine worker, one of the browser's own for a rendered Request (§6.3), claims the Request from the Frontier (with a Lease), first acquires the per-domain concurrency slot, then passes through rate limiting.
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

    # With a Session Pool (§5.3):
    async def accounts(self): ...        # optional: yields the Accounts to bind Sessions to; by default those of the settings
    async def login(self, session, account): ...  # optional: logs a Session in before it is first used, and once logged out
    def is_logged_out(self, response): ...  # optional: whether a Response shows its Session logged out; a 401 by default
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
    render=False,          # True, or browser Render options, see §4.7
    fetcher=None,          # explicit Fetcher; inferred from render and the URL scheme by default
    timeout=None,
    proxy=None, impersonate=None,   # per-Request overrides, rarely needed; proxy="" for none, see §5.3
    ext=None,              # component-private data, by component name
)
```

- Requests are immutable; derive new ones with `request.replace(...)`.
- Without `method`, a Request with a body (`body`, `json`, or `data`) is a POST, and one without is a GET. An explicit method always wins, including a GET with a body, and `replace()` keeps the method as it is.
- `params` adds query parameters to the URL, after those it has and before the fragment: strings and numbers, with a sequence of values repeating a name. Booleans are refused, as sites spell them differently. Like `json` and `data`, it exists only when constructing a Request.
- Requests are independent by default (ADR 0011): no cookies persist from one Request to another. A Request sends only the cookies it carries (`cookies=`, which cannot be combined with a `Cookie` header, and refuses a name that is empty or has `=`, `;`, or a control character, and a value with `;` or a control character). Within one Request's redirect chain cookies flow as in a browser. To continue a conversation explicitly, pass `cookies=response.cookies`; to keep state across many Requests, use a Session Pool (§5.3).
- Kinds: a Request is the kind of request a browser would make it as (`pernix.request_kinds`): a GET is a top-level navigation; a POST whose body is a form (`application/x-www-form-urlencoded`, `multipart/form-data`, or `text/plain`) is a form submission; any other request, such as a POST of JSON, is a `fetch()`. A `Sec-Fetch-Dest` header names the kind instead: `document`, `iframe`, `empty` (a `fetch()` or an `XMLHttpRequest`), `style`, `script`, or `image`; an iframe and the loads are GETs, else `ValueError`, and only a `document` renders. With the pinned Profile, the HTTP Fetcher sends the headers Chrome sends for the kind (§5.4); other Profiles send the Request's headers as they are. No field names the kind, so it persists with the headers.
- The page a Request comes from is its `Referer` header, from which the `Referer`, `Origin`, and `Sec-Fetch-Site` sent are made with the pinned Profile; an empty one names none. With `http.inherit_referer`, a Request constructed while a Callback or a spider Middleware handles a Response, an Inline Fetch included, takes the page that Response belongs to unless it has a `Referer`: the Response's URL when it is a document, a navigation's or an iframe's, rendered ones included, and the `Referer` of its Request otherwise, so the API calls paged from an API Response keep the page that started them. Requests from `start()` and errbacks take none. The setting is off by default, and Requests then carry only the `Referer` they are given.
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
- Responses from the browser also carry `response.browser`: the document as received, captured XHR and fetch responses, screenshots, and the results of `Evaluate` actions; it is `None` for others. With `keep_page` enabled there is also `response.page`, see §4.7.

### 4.5 Callbacks and output semantics

- A Callback can be an `async def` async generator that yields `Request` objects or Items (None is ignored), or a plain `async def` that returns a single object, a list, or None.
- **Output is streamed**: each yielded object is processed immediately. Requests go to the Frontier; Items go into a bounded queue, and when the queue is full the Callback is suspended (backpressure).
- **When a Callback raises** (confirmed):
  - The Request is marked as failed: it is kept as a Dead Request, `RequestFailed` is emitted, and the errback is called. It is **not retried**, because parsing errors are usually deterministic; this holds for every exception a Callback raises, including one from an Inline Fetch, such as `RetriesExhausted` once its own retries ran out. The one exception is a Callback that keeps its page (§4.7) and fails while that page crashed or its browser was lost: the failure is not the Callback's, so it fails with `PageCrashed` or `BrowserLost` instead, and is retried as the fetch would be (§6.4).
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

Browser rendering is expressed with the `render=` parameter (confirmed), which takes `True` or a `Render`. Since it is the same Request type, a download Middleware can upgrade a Request to Render with `request.replace(render=True)` or `request.replace(render=Render(...))`, and the Request goes to the browser Fetcher; custom transports are still selected with `fetcher=`.

```python
from pernix.browser import Click, Render, Scroll, WaitFor

yield Request(
    url,
    callback=self.parse_list,
    render=Render(
        wait_for="div.item",                       # wait for a selector
        actions=[Scroll(times=3), Click("button.more"), WaitFor("div.loaded")],
        block=("image", "media", "font"),          # resource types to block; browser.block by default
        capture="**/api/list*",                    # capture matching XHR and fetch responses
        screenshot=False,
    ),
)
yield Request(url, render=True)                    # render only, no extra actions
```

Page interaction comes in three layers; use whichever you need:

1. **Declarative actions**: serializable, persisted along with the Request, and covering the most common operations (wait, scroll, click, type, run JS, screenshot).
2. **Page methods**: `Render(setup=self.before_goto, action=self.after_load)`. The arguments are Spider methods with the signature `async def x(self, page)` and can run arbitrary patchright code. If one raises, the Request fails (each can be marked optional individually).
3. **Kept page**: `Render(keep_page=True)`; the Callback gets the live `Page` through `response.page`. When the Callback ends (normally, with an exception, or cancelled), the framework **force-closes** the page and its context, and releases the page's slot.

**Render options** (`pernix.browser.Render`, immutable, keyword-only):

- Once the page has loaded, the browser waits for `wait_for`, a CSS selector of an element to become visible, then runs `actions` in order, then takes a whole-page screenshot if `screenshot` is set. Each of these steps may take `browser.action_timeout` (10 s), unless an action sets its own `timeout`; `browser.deadline` still limits the whole fetch.
- `block`: the resource types the page may not load, which the browser aborts as a content blocker would. `None`, the default, takes those of `browser.block`, which is empty, so nothing is blocked by default; a `block` replaces `browser.block` for its Request, without merging, and `block=()` blocks nothing. The types are the browser's: `image`, `media`, `font`, `stylesheet`, `script`, `xhr`, `fetch`, `eventsource`, `texttrack`, `manifest`, and `other`, which covers the kinds the browser names apart, such as `ping` for beacons. A misspelled type is refused with a suggestion; `document` is refused, as the page itself is one, and so is `websocket`, as the browser cannot block a page's WebSockets by type. Blocking `stylesheet` or `script` can keep a page from rendering as it should.
  - Blocking goes by resource type rather than by URL pattern, which would miss resources without an extension: while a page blocks some type, every request it makes goes to Python to be judged, and a page that blocks nothing is not slowed down at all.
  - A page that blocks some type cannot register service workers, as the requests a service worker makes are not judged: registering one never completes. A Session's kept context is used only by fetches that block, or only by fetches that do not, so a fetch that blocks never meets a service worker an earlier fetch registered; a Session switching between the two gets a new context each time, with its cookies and localStorage.
- `capture`: URL patterns, as globs (`*` matches anything but `/`, `**` anything, `{a,b}` either), of the XHR and fetch responses to keep, with their bodies: those that arrived by the time the actions were done, whose bodies then arrive within `browser.action_timeout`, in the order they arrived. A `wait_for` on what the response fills in makes sure it arrived.

**Declarative actions** (`pernix.browser`), immutable and persisted with the Request. Each takes `timeout`, in seconds, and `optional`: an optional action that fails is logged as a warning, gives no output, and the next action runs.

| Action | Does |
| --- | --- |
| `WaitFor(selector, state="visible")` | Waits for the first element matching the selector to be `attached`, `detached`, `visible`, or `hidden` |
| `Scroll(times=1, delay=0.5)` | Scrolls to the bottom of the page `times` times, pausing `delay` seconds after each for the page to load more; the timeout applies to each scroll |
| `Click(selector)` | Clicks the first element matching the selector, once it can be clicked |
| `Type(selector, text, delay=0.0)` | Types the text into the first element matching the selector, key by key, `delay` seconds apart, after what it holds; finding the element and typing share the timeout. The text, which may be a password, is left out of its `repr` and of errors |
| `Evaluate(script, isolated=False)` | Runs JavaScript, an expression or a function to call, in the page, where it reaches the page's globals; with `isolated=True` in a world of its own, which the page's scripts do not see, sharing only the DOM with them. Its result, made JSON-compatible, goes to `response.browser.evaluated` |
| `Screenshot(full_page=True)` | Takes a PNG screenshot, of the whole page or of the window, into `response.browser.screenshots` |

**Errors**: a step that takes longer than its timeout raises `ActionTimeout`, a `Timeout`, so the default retry policy retries it as it does a page that loads too slowly. A step that fails otherwise, such as a script that throws or an invalid selector, raises `ActionError`, a `FetchError` the default policy does not retry, as it would fail the same way again. Both name the step by its place, kind, and selector, such as `actions[1], Click('button.more')`, never by what it types or runs, and `ActionTimeout` is an `ActionError` too.

**Results** (`response.browser`, a `RenderResult`, on every Response of the browser Fetcher):

- `document`: the main document as it came over the network, before its scripts ran, as a `CapturedResponse(url, status, headers, body)`; the rendered HTML is `body`. The Response describes the document the page shows once the actions are done and the captured bodies arrived: when an action leads to another document, such as a `Click` on a link, the Response, its status, headers, and history included, describes the document it led to; a navigation that shows no document, such as one answered with a 204 or a download, leaves the page as it was. A navigation of the page, by an action or by its own scripts, to a document that does not load fails the fetch with the `FetchError` of the browser's error, such as a `ConnectError`, as loading it would, also when a later action then fails on the browser's error page; one the network guard refuses raises `NetworkBlocked`.
- `captured`: the `CapturedResponse`s of `capture`.
- `screenshots`: PNGs in the order taken, that of `screenshot=True` last.
- `evaluated`: the results of the `Evaluate` actions, in order.

Bodies are decoded from their `Content-Encoding`, and the headers that describe the encoded body are then left out.

**Page methods** (`setup` and `action` of `Render`): Spider methods, `async def x(self, page)`, given the patchright `Page`, for what the declarative actions cannot do. `PageMethod(method, optional=False, timeout=None)` (`pernix.browser`) marks one optional, so that its failure is logged as a warning and the fetch goes on, or limits its time; without a `timeout`, only `browser.deadline` limits it. Their return values are ignored.

- `setup` runs once the context holds the Request's cookies, before the page loads. The routes it adds are tried after those of the Fetcher, which were added later: resource blocking, the rewrite of the first request, and capture apply whatever it routes.
- `action` runs once the page has loaded, after `wait_for` and the actions, and before the screenshot and the captures are gathered; the Response describes the page as it left it.
- A method that takes longer than its `timeout`, or whose call to the browser times out, raises `ActionTimeout`; one that raises otherwise raises `ActionError`, naming it, such as `Render(action=Books.after_load)`, with its exception as the cause. `Retry`, `IgnoreRequest`, and the errors of Pernix go through as they are. A page that crashes, or a browser lost, during a method fails the fetch with `PageCrashed` or `BrowserLost`, as at any other point.
- They run in rendered Inline Fetches too.
- They persist by name, as Callbacks do: the name, and the `optional` and `timeout` of a `PageMethod`; a method that is not the Spider's cannot be persisted, and one missing when the Request is restored fails it at once with `SerializationError`.

**Kept pages** (`Render(keep_page=True)`): the Callback gets the page, open, as `response.page`, to work with it, such as clicking through it.

- The page is kept for the Callback of a Request the Run fetches, from the Frontier (`pernix.fetchers.kept_pages`): once its attempt ends, whether the Callback returned, raised, or was cancelled, as by a Force Stop, or a retry was decided instead, the page is closed with its context, and its slot given back once they are closed. Elsewhere a Request that keeps its page raises `ValueError`: in an Inline Fetch, the shell, and a Fetcher called directly outside `keeping_pages()`. `pernix fetch` runs its URLs as a Run, whose Requests keep no page unless a download Middleware of the project makes them, and then the page is closed once the Response is reported. A Fetcher that is not the browser's, such as a `FakeFetcher` standing in for it, gives no page.
- The page counts toward `browser.max_pages` while it is kept, and keeps its browser from closing as it is replaced (§5.2). The rendered fetches made while it is kept, by the Callback or the tasks it starts, Inline Fetches included, share its slot rather than wait for one, as a Callback holding the last page would otherwise wait forever; the pages open may then exceed `browser.max_pages` by those fetches.
- Under a Session, what the fetch changed is kept in the Session when the fetch ends, as for any fetch, but not what the page changes while the Callback works with it; the page's context is closed after the Callback and never used again, so the Session's next fetch gets a new one from its state (§5.3).
- A Callback that fails while its page crashed, or its browser was lost, fails with `PageCrashed` or `BrowserLost`, caused by its own error, and the retry policy judges it as it would the fetch failing so (§4.5, §6.4).

**Persistence and Fingerprints**: a `Render` and its actions persist with the Request, each action tagged with its kind. Rendering, and its options, take no part in the default Fingerprint (§6.2): a Request switched to Render, or rendered with other options, is not fetched again.

Other conventions:

- Pages are closed by the Fetcher, kept pages by the Run once their Callback ends. Pages that timed out or were cancelled are treated as "dirty pages" and are always closed directly, never reused.

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
- Environment variables map `PERNIX_DOMAIN__DELAY` to `domain.delay`; their string values are parsed by the declared type, and container values are written as JSON, unless the type also takes a string, as `locale` does, in which case text that is not JSON stays a string. Values from Python layers are type-checked strictly.
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
| `pernix fetch URL... [-i FILE] [--fetcher NAME] [--timeout S] [--json] [-o FILE]` | Fetch URLs as one small Run and print each Response |
| `pernix fetch URL [--extract text\|markdown] [--css SEL] [--xpath EXPR] [--links] [--max-chars N]` | Read pages as agents and scripts need them: text, Markdown, selected parts, or links, bounded in size |
| `pernix shell [URL]` | Interactive console with top-level `await`, and `request`, `response`, `settings`, and `fetch()` preloaded |
| `pernix check [TARGET]` | Validate settings and components (and, with the browser Fetcher, the browser environment) |
| `pernix run ... --job-dir DIR` | Resume a Run (planned, with persistent Frontiers) |
| `pernix new NAME` | Generate a project skeleton (planned; project mode does not enforce a directory layout) |
| `pernix browser install [--with-deps]` | Install the browser engine the browser Fetcher launches |

- Every command but `browser install` takes `-s KEY=VALUE`, the highest settings layer (§4.11). `run` and `check` take `-a KEY=VALUE` Spider arguments. Results go to stdout; messages and logs go to stderr.
- TARGET is a `.py` file, imported under its stem with its directory first on `sys.path`, or an import path such as `myproject.spiders.books`. As with `python -m`, the working directory comes first on `sys.path`, so the project's own modules, Spiders and components alike, import without being installed. `:Name` picks a Spider by class name or `Spider.name`; it may be left out when the module defines exactly one Spider, not counting those it imports.
- Exit codes are those of `run()`: 0 completed, 1 failed, 2 configuration error (including invalid options, and a TARGET that cannot be found), 130 stopped by a signal.
- `fetch` and `shell` fetch through the download Middleware and the Fetchers of the project settings. `shell` fetches each Request once, without retries, a Frontier, or Pipelines. `fetch` fetches its URLs as one small Run of a Spider of its own, so the retry policy, the per-domain limits, and `concurrency`, with `browser.max_pages` for rendered pages, apply, as in any Run; the project's Feeds, spider Middleware, and Pipelines, which have no Items to handle, are left out, and the Run logs only critical records, as `fetch` reports each URL itself (`-s log.level=INFO` shows the rest). `-s retry.times=0` makes one attempt. `--fetcher` names a registered Fetcher, such as `browser`, which renders the page; otherwise the URL scheme picks one. Impersonation is on by default with the pinned Profile (§5.4); `-s http.impersonate=...` changes it.
- For one URL, `fetch` writes the status line, the redirects, and the headers to stderr, and the body to stdout: decoded text for text media types, raw bytes otherwise, though never to a terminal; or what was read from it (below). `-o FILE` writes the body byte for byte. A configuration error raised while fetching, such as patchright or the browser engine not being installed, is not a failed fetch: it is written to stderr, even with `--json`, stops the Run, and exits with 2.
- Several URLs, given as arguments or with `-i FILE` (`-` for stdin, one URL per line, blank lines and those starting with `#` skipped), need `--json`, and `-o` takes one URL. URLs given as arguments that are not absolute are a configuration error, and so is an invalid selector, before anything is fetched; a line of `-i` that is not a URL is reported as that URL's failure. A URL given twice is fetched twice.
- `fetch` exits with 0 when every URL got a Response, whatever its status, the last one included once the retries of its status ran out; with 1 when any did not, as when fetching failed, a download Middleware ignored it, the Run ended before fetching it, or stdout was closed; with 2 for a configuration error; and with 130 when a signal stopped it.
- Reading, for agents and scripts that pay for every character:
  - `--extract text` reads the visible text, as a browser's `innerText` lays it out: without scripts, styles, media, and hidden elements (the `hidden` attribute, or an inline `display: none` or `visibility: hidden`), block elements on their own lines, table cells separated by tabs.
  - `--extract markdown` reads the main content as Markdown: the `<main>` (or `role="main"`) holding text, else the only `<article>`, else the body. Navigation, asides, dialogs, search forms, the elements with their ARIA roles or with classes or ids naming boilerplate (such as `cookie-banner`, `share`, or `sidebar`, unless they also name content), and, in a body, its `<header>` and `<footer>` and the blocks made mostly of links are dropped. Headings, paragraphs, lists, block quotes, tables with a header row, code blocks with their language, emphasis, inline code, links, and images with alt text are kept, URLs made absolute against the document's base URL. Extraction is built on lxml, with no dependency of its own, and applies to HTML documents; other text bodies are read as they are.
  - `--css SELECTOR` and `--xpath EXPR`, repeatable and kept in order, read the parts they pick instead of the page: an element as its markup, or as its text or Markdown with `--extract` (without main-content detection), and a string, such as `::text` or `@href`, as it is. They apply to HTML and XML documents.
  - `--links` reads the http and https links instead, `<a href>` and `<area href>`, of the page or of the parts the selectors pick, as absolute URLs with their text (else their `aria-label`, `title`, or image alt text), each URL once.
  - `--max-chars N` reads N characters at most: the body is cut, the parts are kept while they fit, the last one cut, and the links while their URLs and texts fit.
  - Without `--json`, the body or its extraction is written to stdout, the parts one per line, the links as `URL<TAB>text` lines, and a cut is noted on stderr.
- `fetch --json` writes one JSON object per URL, a line each, as it ends (JSON Lines). A Response is `schema` (1, raised when a field changes meaning or goes away), `url` (requested), `final_url`, `status`, `http_version`, `headers` as `[name, value]` pairs in order, `encoding`, `elapsed` in seconds, `redirects` as `{url, status}` objects, `fetcher`, `body` (the decoded text, or what `--extract` made of it; `null` for a binary body, one written with `-o`, and when selectors or links are read), `body_bytes`, `matches` (the parts the selectors picked) or `links` (`{url, text}` objects) when asked for, and `truncated`, whether `--max-chars` cut anything. A failure is `{"schema": 1, "url": ..., "error": {"type": ..., "message": ..., "code": ...}}`, where `code` is the transport's error code, such as libcurl's; once retries ran out because of an exception, it is that exception, a Request a download Middleware ignored, or a component vetoed, is an `IgnoreRequest` or a `RequestDropped`, and one the Run ended before fetching, as when it drained for want of proxies, is a `NotFetched`.
- Pointed at untrusted links, `-s network.public_only=true` refuses hosts whose addresses are not public, after DNS resolution, on every redirect hop (§5.1), the browser's included, through a proxy that checks every host it reaches (§5.2); `network.allow_hosts` names hosts it lets through. The body limit is `http.max_body_size`, enforced as the body streams in.
- In `shell`, `await fetch(url_or_request, **options)` returns the Response and rebinds `request` and `response`; the keywords go to `Request`, or to the `replace()` of a Request given, so `await fetch(url, render=True)` renders the page, and `Request` is preloaded too. Ctrl-C cancels what is being awaited, and Ctrl-D leaves.
- `check` builds every component, including the `setup()` of the Addons, without opening any, so import paths, component roles, chains, and Feed paths are all checked, and prints a summary of the project file, the Spider, and the components. Every problem is reported at once: the settings are validated together, and so are the components that do not depend on one another, as when a Run starts.
- With a browser Fetcher registered, as it is by default, `check` then launches the browser once, as the first rendered Request would, with the `browser.*` settings, and closes it; the summary names the browser and its version, and tells whether it is the bundled build, as it tells whether the HTTP Fetcher's Profile is the pinned one (§5.4). An engine that is not installed is a problem, reported with the command that installs it, `pernix browser install`, and so is a browser that does not start. Without patchright, the `browser` extra, the summary only notes it, with the command that installs the extra: a project that never renders does not need it.
- `browser install` runs patchright's installer for the browser engine the browser Fetcher launches, Chrome for Testing, without the headless shell it never uses; an engine already installed is kept. `--with-deps` also installs the system libraries the browser needs, on Linux. It exits with 0 once the engine is installed, 1 when installing it failed, and 2 without patchright.

## 5. Fetching layer

A Fetcher is a component with `async def fetch(self, request, *, session=None) -> Response` and a `schemes` attribute, the URL schemes it fetches by default. It may also have `stream(request, *, max_bytes, session=None)`, an async context manager giving a `StreamedResponse`, for streamed Inline Fetches (§4.6). `session` is the Session the Request is sent under, which the Session Pool passes only for such Requests (§5.3): a Fetcher that never sends Requests under a Session may leave the parameter out, and one that cannot refuses them. Fetchers are registered by name with the `fetchers` setting, `{"http": HTTPFetcher, "browser": BrowserFetcher}` by default; registering another component under a built-in name replaces it, and `None` (`false`) disables one. The innermost step of the download chain picks the Fetcher: the one named by `Request(fetcher=...)`, otherwise `browser` if the Request needs rendering, otherwise the one whose `schemes` include the URL's scheme. No two Fetchers may claim the same scheme, and a Request no Fetcher fits fails. `Response.fetcher` records the name. Like other components, Fetchers get services injected and are opened and closed with the Run.

### 5.1 HTTP Fetcher (curl-cffi)

The `HTTPFetcher`, registered as `http`, registers the `http.*` settings.

**curl session pool**

- Underlying curl sessions are grouped and reused by `(Session, Profile)`, Requests without a Session sharing the ones of their Profile, created on first use inside the running event loop, and evicted by LRU beyond `http.pool_size` (8) plus an idle timeout of `http.pool_idle` (300 s); a session in use is never closed, one idle too long is replaced rather than reused, and retired sessions close in tasks of their own, which closing the Fetcher waits for. A single underlying curl session never mixes different Profiles or Sessions, so connections are never shared between them.
- impersonate is set explicitly on every Request, so a reused connection handle never carries over the previous Request's Profile. The Profile is `http.impersonate`, or `Request(impersonate=...)`; both must name a pinned target (§5.4).
- Cleartext `http://` Requests use HTTP/1.1, as browsers never upgrade those to HTTP/2; HTTPS negotiates as the Profile does. Each redirect hop picks its own, so a redirect from `http://` to `https://` gets HTTP/2 from a server that offers it.

**Timeouts and cancellation**

- `http.timeout` (30 s), or `Request(timeout=...)`, limits connecting and waiting for the response headers, those of each redirect hop apart, precisely, with asyncio, and then each pause in the body, to the nearest second, with curl. `http.deadline` (180 s) limits the whole fetch, waiting for a connection and reading the body included, so a server trickling data cannot hold a worker forever.
- Every transfer is streamed from curl. A fetch that is cancelled, times out, or stops reading early, before or after the headers arrived, stops its transfer: curl drops the handle and its unfinished connection, and the pool stays usable.
- curl-cffi exceptions are mapped onto the framework's own exception hierarchy, `FetchError` and its subclasses `Timeout`, `ConnectError`, `ProxyError` (raised too for a proxy that cannot be reached, as curl connects to the proxy alone, and for a 407 response from a proxy), `TLSError`, `TooManyRedirects`, `DecodeError`, and `ResponseTooLarge`; the original curl error code is kept in `code` (0 for limits the framework enforces), and the curl-cffi exception is the `__cause__`.

**Concurrency**

- Per-host connection limits are implemented by the framework's concurrency slots, not by curl's multi-handle options; each curl session allows `concurrency` transfers in flight.
- HTTP/2 connection reuse is enabled (`PIPEWAIT`).

**Data**

- Request headers preserve order and case, and support duplicate keys; they follow the Profile's own headers. With the pinned Profile, the headers are those Chrome sends for the Request's kind (§4.3, §5.4), in Chrome's order: a Request's own header of one of their names replaces it in place, keeping its case, and another goes after them in a navigation and first among those a page adds in the other kinds, those of one name together. Its `Referer` and `Sec-Fetch-Dest` name the page it comes from and its kind instead of being sent as they are, and its `Origin` is made from the page too. Over HTTP/1.1, `Host` and `Connection: keep-alive` come first, and names keep Chrome's case. A `fetch()` to another origin than its page's sends no cookies of the Session or of the chain, only the Request's own, and the cookies its responses set are not kept, as `fetch()` sends cookies to its page's origin only.
- Response headers are read with all multiple values preserved. A body decoded from gzip, deflate, br, or zstd loses `Content-Encoding` and `Content-Length`, which describe the encoded body; a `HEAD` Response keeps them.
- Redirects are followed by the Fetcher itself, as browsers follow them, one curl transfer per hop, up to `http.max_redirects` (20, as browsers do), more raising `TooManyRedirects` (code 0) before the next hop is sent, and recorded in `Response.history`. Following the Fetch standard's HTTP-redirect fetch as Chrome applies it to navigations (`pernix.fetchers.redirects`):
  - Only 301, 302, 303, 307, and 308 redirect, with the first non-empty `Location`, its bytes beyond ASCII percent-escaped, resolved against the URL that answered as WHATWG URLs resolve it; other 3xx responses, and redirects without a `Location`, are final.
  - 301 and 302 turn a POST into a GET, and 303 anything but GET and HEAD, without the body and the headers that describe it (`Content-Type`, `Content-Encoding`, `Content-Language`, `Content-Location`); 307 and 308 keep the method and the body.
  - `Authorization` and `Proxy-Authorization` are dropped on a redirect to another origin, another scheme, host, or port, and stay dropped for the rest of the chain. Other headers go to every hop, as in the browser.
  - A `Location` that is not a valid URL (code 3), or names neither an http nor an https URL (code 1), fails the fetch with a `FetchError`, which is not retried, as browsers refuse to follow it.
  - The body of a redirect is dropped, and never counts toward `http.max_body_size` or `max_bytes`.
  - Each hop passes the network guard before it is sent (§9), in one place every hop goes through, so other checks of the hops can join it, as the address guard does.
- The address guard, on with `network.public_only` (off by default), keeps untrusted links off private networks (`pernix.fetchers.address_guard`). Before each hop is sent, the first included, its host is checked: an address as it is, in any form curl and browsers read addresses in, such as `127.1` or `0x7f000001`; a loopback name, `localhost` or `*.localhost`, which curl resolves itself, refused, and so is a host WHATWG URLs reject; any other name resolved, with the hop's timeout, every address it resolves to having to be public. A public address is a globally routable one: not private, loopback, link-local, unspecified, shared (100.64.0.0/10), reserved, documentation, or multicast, nor an IPv6 address standing for such an IPv4 one (mapped, or NAT64). curl then connects only to the addresses checked, given as `CURLOPT_RESOLVE` entries that expire as its cached ones do, so a name answering otherwise the next time, as in DNS rebinding, cannot reach another. A refused hop raises `NetworkBlocked`, which is not retried, naming the address and, for a redirect, where it came from; a name that does not resolve raises `ConnectError` (code 6), as curl does. Through a proxy, which resolves names itself, only hosts written as addresses, and loopback names, are checked; but curl resolves names itself for a `socks4://` or `socks5://` proxy, handing it the address, so through one those are resolved, checked, and pinned as without a proxy, while `socks4a://` and `socks5h://` proxies resolve them. The hosts of `network.allow_hosts`, matched exactly, are not checked. The browser Fetcher reaches hosts through a proxy that checks them alike (§5.2).
- International hosts: curl converts a host to ASCII itself, differently on each platform (on Linux, libidn2's older Unicode tables, retrying with the transitional mappings when the non-transitional conversion fails), so before curl sees a Request's URL, the Fetcher converts the host curl connects to, the one after the last `@` of the authority, when it is not ASCII or holds percent escapes, as WHATWG URLs convert hosts (`pernix.urls.with_ascii_host()`): percent-decoded, without the transitional IDNA mappings, and IPv4 addresses in dotted decimal. Every platform then reaches the host a browser does, `STRAẞE.de` being `xn--strae-oqa.de`, not `strasse.de`, and `Response.url` names it in ASCII. A host WHATWG URLs reject, such as `a<ZWJ>b.example`, fails the fetch before connecting with a `FetchError` (code 3, as curl's own malformed URLs), which is not retried, as browsers refuse such a URL. Redirects are resolved as WHATWG URLs resolve them, so their hosts are converted alike.
- A body larger than `http.max_body_size` (100 MiB; 0 for no limit) fails with `ResponseTooLarge`: at once when `Content-Length` declares it, unless the response has no body (`HEAD`, 1xx, 204, 304), otherwise as soon as curl receives more, counted from the first byte in curl's write callback, however slowly the body is read.
- Streaming downloads: `async with fetcher.stream(request, max_bytes=...) as streamed:` gives a `StreamedResponse` whose `response` holds everything but the body, marked `streamed`, read once with `iter_bytes()` or written to a file with `save(path)`. Only the last body of a redirect chain is streamed. `http.deadline` then limits only the wait for its headers. curl stops receiving while 1 MiB of the body waits to be read, and resumes once half of it has been, so a slow reader holds a bounded part of the body in memory, whatever `max_bytes` is; a paused transfer never times out. The chunks curl keeps while paused count once toward `max_bytes`. Callbacks reach it with `self.stream()` (§4.6).

**Delegated to the framework**

- Without a Session, no cookies persist between Requests (ADR 0011): each fetch starts with only the Request's cookies, and cookies set during it, including on redirects, go to `Response.cookies` instead of shared state. Connections are still reused. With a Session, its cookies are managed by the framework, see §5.3.
- Each hop of a redirect chain gets its cookies from a cookie jar of the fetch (`pernix.fetchers.chain_cookies`), which matches them to its URL by the jar's rules (§5.3): domain and host-only flag, path, Secure, and expiry. It starts as the Session's cookies, if any, joined by the Request's own, and the `Set-Cookie` headers of each hop join it, by the jar's rules too. They are sent in the order Chrome sends them, longer paths first, then the earliest set first, a cookie that replaces another keeping its place, as `CURLOPT_COOKIE`, which curl sends as one `Cookie` header over HTTP/1.1 and as a `cookie` field each over HTTP/2, as Chrome sends them. Each hop's URL is quoted as curl-cffi quotes it before its cookies are matched, so `/café` matches cookies of `/caf%C3%A9`, the path sent. `Set-Cookie` values are read as UTF-8, unless their bytes are not UTF-8, then as Latin-1, and cookies are sent encoded as UTF-8, so those received in UTF-8 go back byte for byte and a Session holds them as text. Cookies a header cannot carry, with a control character other than the tab, are left out. curl's own cookie engine, which orders cookies by path, domain, and name length, and sends a `Cookie` header next to the cookies it set on the way, adds none.
- The Request's cookies, given as `cookies=` or as a `Cookie` header, which is not sent as given, are held for its host only, for every path, naming the host the Fetcher gives curl (lower-cased, IPv6 without brackets, and in ASCII, so `faß` is `xn--fa-hia`): they go to every redirect hop on that host, whatever its port or scheme, and to no other host, in the order given. Names with the `__Secure-` or `__Host-` prefix are Secure, as browsers require, so they go over HTTPS, and to loopback hosts, only.
- A Request sent under a Session also sends the Session's cookies; a cookie of the Request replaces the Session's held for its host and the path `/`, in its place. As the headers of each hop arrive, so even when a later hop or the body fails, its `Set-Cookie` headers are applied to the Session's cookie jar, with the hop's URL and the time they arrived, by the jar's own rules, so attributes curl does not keep, such as `SameSite`, are kept; those of informational (1xx) responses and of a proxy's answer to `CONNECT` are not applied, and the Request's own cookies are not kept. The Session's proxy and Profile apply unless the Request names its own, an empty proxy for none, and `Response.session` is the Session's id.
- Retries are handled by the framework; curl-cffi's built-in retries are turned off, and so are proxies from the environment, `NO_PROXY` included, so it never bypasses a proxy a Request names.

### 5.2 Browser Fetcher (patchright)

**Topology**

- The browser Fetcher starts patchright with the first Request that needs rendering, and it launches one browser with `launch()`, so a Run that renders nothing never starts them; both are closed with the Run, or replaced by new ones, as Generations (below) are. Without `pernix[browser]` installed, or without the browser engine, that Request fails with a `ComponentError` naming the command that installs it, `pernix browser install` for the engine, and so do the later ones, at once; an `executable_path` in `browser.launch_options` that does not exist is named instead.
- The browser is the Chrome for Testing build bundled with the pinned patchright, never the headless shell. In headless mode it names itself as it does with a window, and it presents the languages of `locale` (§5.4). It renders top-level navigations only, and loads them as typed in: a Request's `Referer` and `Sec-Fetch-Dest` are not sent, as a navigation the browser starts with a `Referer` would name a page without coming from it, so the HTTP Fetcher and the browser send a Request with a `Referer` differently. It creates multiple contexts, kept per Session (§5.3); without a Session, each rendered Request gets a fresh context, closed after the fetch, so Requests stay independent as on the HTTP side.
- A context's proxy, locale, and time zone are set once at creation and never modified afterward, and so is the localStorage a Session's context starts with; switching proxies means creating a new context. A Session's cookies are synced into its context before each fetch (§5.3). `Request(proxy=...)` is the proxy of its context, for every request of it, loopback ones included, in place of the proxy the browser was launched with, if any; the browser's own requests, which belong to no context, go through the latter. Chrome cannot go through a SOCKS proxy with credentials, so a Request that would is refused with a `ValueError` before anything is launched.

**Loading a page**

- A page is loaded until its `load` event, within `browser.timeout` (30 s), or `Request(timeout=...)`; `browser.deadline` (180 s) limits the whole fetch, starting the browser included, from when it may open its page (see Capacity). A fetch that is cancelled or times out closes its context in a task of its own, which closing the Fetcher waits for.
- The cookies a Request carries (`cookies=` or a `Cookie` header) are added to its context as host-only cookies of its host, for every path, in the order given: they go to every redirect hop on that host and to no other host, and cookies set while the page loads flow as in the browser. The browser takes cookies named with the `__Secure-` or `__Host-` prefix only as Secure ones, so those go over HTTPS, and to loopback hosts, only.
- A Request other than a plain GET, or with headers, changes the first request of the page: its method, body, and headers. The browser keeps those headers on the redirects that follow, to any host, as the HTTP Fetcher does, so the browser Fetcher refuses `Authorization` and `Proxy-Authorization` headers, which the HTTP Fetcher drops on the way to another origin. The method and body follow the browser's redirect rules, and the page's own requests are untouched.
- The body of an HTML or XHTML document is its rendered DOM, encoded as UTF-8 and decoded as UTF-8, without the `Content-Length` and `Content-Encoding` that describe the body sent. Chrome shows other documents, such as JSON, text, or images, in a viewer page of its own, so their body is the one received. A document the browser downloads instead of showing fails with a `FetchError`, and a 204 or 205 gives an empty body.
- `status` and `headers` are those of the document as the browser reports them: every value is kept, but not their wire order. `history` holds the redirects of the navigation, `cookies` those the context holds for its URLs, `ip_address` the server's address; the browser does not report `http_version`.
- Chrome's network errors (`net::ERR_*`) map onto `Timeout`, `ConnectError`, `ProxyError` (`ERR_PROXY_*`, `ERR_SOCKS_*`, and a tunnel that failed), `TLSError`, and `TooManyRedirects`, and others onto `FetchError`, with `code` 0 and the error's name in the message.

**The address guard** (`pernix.fetchers.address_guard_proxy`)

- Chrome resolves the hosts it reaches itself, out of the reach of a check made before the fetch, and a page reaches hosts of its own choosing. So while `network.public_only` is on, the browser is launched with a proxy that takes all its traffic: the documents of its pages, their subresources, redirects, and WebSockets, and its own background requests. Its bypass rules send loopback hosts to it too.
- The proxy is a SOCKS5 one, on 127.0.0.1, rather than an HTTP one handling `CONNECT` and plain HTTP: Chrome hands it each host as the URL names it, never resolving it itself, one host per connection, and the bytes on it are those Chrome would send on a connection of its own, while an HTTP proxy gets plain HTTP requests for many hosts on one kept-alive connection, which it would have to parse and rewrite, and could refuse one only by closing the connection, which Chrome retries, or by an answer the page would take for the site's. For each connection, the proxy checks the host by the rules of the address guard (§5.1): an address as it is, a loopback name refused, any other name resolved once, every address having to be public, and the connection made only to the addresses checked, in the resolver's order, the next tried as soon as one fails or once it has not connected within 250 ms, as browsers do, so a name answering otherwise for another connection, as in DNS rebinding, reaches nothing it did not check. A host's addresses, once checked, are kept for a minute, as Chrome keeps the hosts it resolves, so the connections a page makes to one host meanwhile go to one address. The hosts of `network.allow_hosts` are resolved and reached whatever their addresses; resolving and connecting take 30 s at most. Under the network guard of tests too, the proxy reaches only the hosts it admits (§9).
- WebRTC sends UDP, which no proxy carries, around the proxy, so the browser is launched with `--webrtc-ip-handling-policy=disable_non_proxied_udp`: WebRTC then goes through the proxy over TCP, or nowhere. WebTransport, which goes over QUIC, fails through a proxy.
- Whatever the reason, a connection the proxy does not make fails in the browser with `net::ERR_SOCKS_CONNECTION_FAILED`, so the proxy keeps why, by host and port, before it answers, and the Fetcher tells it: a refused document, or redirect, raises `NetworkBlocked`, worded as the HTTP Fetcher's, as does a navigation of the page to a refused host, once it loaded; a host that does not resolve, or does not answer, raises `ConnectError` (code 6 or 7, as curl's) or `Timeout`. A Request's host written as an address, or a loopback name, is refused before the browser is launched or a page slot taken. Any other request refused, a subresource or a WebSocket, fails in the page, which renders on without it, as a browser shows a page whose images did not load; each is counted as `browser.requests_refused`, and logged at DEBUG with its URL, the page, and why.
- `Response.ip_address` is the address the proxy connected the document's host to, rather than the proxy's, which Chrome reports.
- The proxy of a context replaces the one the browser was launched with, so a page would reach hosts through a Request's own proxy, or its Session's, unchecked: such a Request is refused with `NetworkBlocked` before a page slot is taken, without naming the proxy. `Request(proxy="")` renders without one. Likewise, `browser.launch_options` that would take the browser around the proxy, a `proxy` of its own, `ignore_default_args: true`, or Chrome switches in `args` or `ignore_default_args` that set or skip a proxy, or set what WebRTC does, raise a `SettingsError` when the Fetcher is created.
- Each Generation has a proxy of its own, closed with it, which closes the connections it still carries; the proxy runs in the Run's event loop.

**Capacity**

- At most `browser.max_pages` (8) pages are open at once, each that of one fetch: a fetch takes a page slot before it starts the browser or creates its context, waiting in the order the fetches asked, and gives it back once its page is closed, so a fetch cancelled or past `browser.deadline`, whose context closes in a task of its own, holds it until the context is closed; waiting for one takes no part in `browser.deadline`. A slot is an object of its own, so a page kept open past its fetch for its Callback (§4.7) holds it until the page is closed; the rendered fetches made while it is kept share it, so the pages open may then exceed `browser.max_pages`.
- Every fetch has a context of its own, kept for its Session or fresh (§5.3), so the contexts in use are bounded by `browser.max_pages` too, and those kept idle by `browser.max_idle_contexts`; there is no other limit on contexts, nor on the pages of one.
- A Run's rendered Requests have `browser.max_pages` workers of their own, apart from the `concurrency` workers that take the other Requests, so neither kind waits for the other (§6.3).
- Stats: `browser.pages` and `browser.contexts`, the pages and contexts opened; `browser.pages_open` and `browser.contexts_open`, how many are open; `browser.page_waits` and `browser.page_wait_seconds`, the fetches that waited for a page, and how long.

**Generations** (`pernix.fetchers.browser_generations`)

- Each browser runs with a patchright driver of its own; the two are one Generation, from the browser's launch until it is closed. One takes the new fetches at a time, the current one; the first fetch that finds none launches one, while the others wait for that launch, so a browser is started once, whether it is the first, a replacement, or a restart.
- A Generation is replaced once it is `browser.generation_age` (3600 s) old or opened `browser.generation_pages` (1000) pages, as the next fetch finds, or once the resident sets of its processes, the browser's and those it started, sum up to more than `browser.generation_memory` MiB (0, not measured, by default), as its health probe finds. 0 turns each off. The fetches in it finish there while the next go to a new one, and it is closed once the last one ends; its contexts kept for Sessions are closed at once, or once their fetch ends, and the Sessions get new ones in the next, from their state. Measuring the memory takes psutil, which the `browser` extra installs.
- Every 10 s, each Generation's health probe asks the browser for its version, through the driver; the probe also measures its memory, if `browser.generation_memory` is set.
- Once closed, what is left of a Generation's processes is killed, so a driver that died, which leaves its browser running, leaves no process behind: the browser's process group, which it leads, and the processes it started, as found then, or, should the browser have ended before them, as its health probe last found them. Each is told by its id and when it was created, so a process that took the id of one that ended is left alone.
- Stats: `browser.generations`, the browsers launched; `browser.replaced` and `browser.replaced_by_reason.<age|pages|memory>`; `browser.lost` and `browser.lost_by_reason.<disconnected|driver|unresponsive>`; and `browser.page_crashes`.

**Failures**

| Failure | Handling |
| --- | --- |
| Page crash | The page's `crash` event, or patchright's error for it, ends the fetch with `PageCrashed`, a `FetchError` the default retry policy retries as an ordinary retry, as the page may crash again; a context kept for a Session is closed once what the fetch changed is kept |
| Browser disconnected | The Generation is lost: its fetches are cancelled and end with `BrowserLost`, which the default retry policy retries at once under a retry budget of its own (§6.4), and the next fetch launches another |
| Driver process dies | No event tells of it: an error of a fetch that names the closed connection, or a driver process that ended, as its health probe finds, loses the Generation as above |
| Browser or driver stops answering | A health probe that gets no answer within 10 s loses the Generation as above, and its driver is killed, so that what waits for it, cancelled or not, ends |

patchright may fail a call before it emits the event of what failed it, so an error that names a closed page, browser, or driver waits up to a second for the event, and becomes `PageCrashed` or `BrowserLost` if it comes.

**Defaults**

- `headless=True` (`browser.headless`), using the new headless mode; the UA is not overridden.
- `browser.launch_options` are passed to patchright's `launch()` as they are, over the defaults, for control the settings do not give, such as `executable_path` or `args`.
- A `stealth` preset is also provided: system Chrome, headed mode, no viewport set.
- Resource blocking is off by default: `browser.block`, the resource types no rendered page may load, is empty. `Render(block=...)` replaces it for one Request, without merging, and `Render(block=())` blocks nothing (§4.7).
- `browser.action_timeout` (10 s) limits `Render(wait_for=...)` and each page action that sets no `timeout` of its own.

### 5.3 Sessions and Cookie interoperability

- **What a Session contains**: a Session holds Cookies and browser storage state, the client hints each origin asked for (§5.4), a bound proxy, a Profile, and an optional reference to an Account. The HTTP side and the browser side are both just caches of the same Session state.
  - **HTTP side**: Cookies are written before a Request is sent, and changes are merged after the Response arrives. curl-cffi's Cookie format loses attributes such as SameSite, so the Fetcher applies the `Set-Cookie` headers to the Session itself, keeping that metadata (§5.1), and so does it the `Accept-CH` of top-level navigations (§5.4).
  - **Browser side**: a rendered Request under a Session is loaded in a browser context kept for that Session, which one fetch uses at a time; concurrent fetches of a Session get one each.
    - A context is created with the Session's localStorage and the proxy of the Request or of the Session, which, like its locale and time zone (§5.4), never change afterwards: a Session whose proxy changes, or whose localStorage changed elsewhere, gets a new context, and its kept ones that no longer fit are closed.
    - Before each fetch, the context is made to hold the Session's cookies that have not expired, with the Request's own for this fetch only, and no other, adding and expiring only those that differ.
    - After it, whether the page loaded or not, what the page changed is read, and the pages of the context closed, the page's and those it opened, as the context is the fetch's alone; only then are the changes kept in the Session, so they are saved with the fetch's `StateDelta` while the Lease is still held: the cookies it set, changed, or removed, and the localStorage of the page's own origin. A fetch cancelled before then keeps nothing, and its context is closed. The localStorage of frames from other sites, which Chrome partitions by top-level site, is not kept, and neither is IndexedDB.
    - The client hints the responses to the navigations of the page's main frame ask for with `Accept-CH`, its redirects included, are kept with the rest. Before each fetch, the context is taught the client hints of each of the Session's origins that it does not keep, as they were asked for over HTTP or forgotten since (§5.4).
    - A page kept for its Callback (§4.7) is left open: what the fetch changed is kept all the same, but not what the page changes while the Callback works with it, and its context is closed after the Callback, never used again.
    - Cookies map between the jar and the browser by name, domain, host-only flag (a domain cookie's domain has a leading dot in the browser), and path, with expiry, Secure, HttpOnly, and SameSite. Only the attributes the browser reports changed are taken from it; the others, such as an unspecified SameSite, which the browser reports as Lax, and the time a cookie was first set, are kept from the jar. A cookie the page set in place of one of the Request's own, which the browser held instead of the jar's, takes every attribute from the browser. A cookie the page made without SameSite is held as Lax. Partitioned cookies are left to the browser.
    - At most `browser.max_idle_contexts` (8) contexts are kept idle between fetches, the least recently used closed beyond that, and one idle for `browser.context_idle` (300 s) is closed; those in use are one per fetch, so at most `browser.max_pages` (§5.2). A Retired Session is never leased again, so its contexts go the same way.
- **Types** (`pernix.Session`, `pernix.CookieJar`, `pernix.Cookie`):
  - A `Session` has an `id` (random unless given), its `cookies` (a `CookieJar`), `local_storage` by origin, `client_hints` by origin, each the names of the hints the origin asked for, lower-cased, in the order Chrome keeps them, and an optional `proxy`, `profile`, and `account`. `Session.state()` and `Session.from_state(id, state)` convert it to and from a `SessionState`, a msgspec Struct, as are the `Cookie` records, so state is stored without pickle (ADR 0004).
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
  - **Accounts**: with Accounts, the pool holds one Session per Account, under the Account's id, and no other; `sessions.size` and `sessions.eager` do not apply. An `Account` (`pernix.Account`, a msgspec Struct) has a `username`, a `password`, an `id` (the username unless given), `extra` credentials by name, such as a TOTP seed, and a `max_concurrency`, 1 by default, so its Session takes one fetch at a time. The password and the `extra` credentials are `Secret`s, given as such or as strings; their values are masked in the Run's log, and the Session's state names its Account by id only, so credentials are never stored with cookies. They come from `Spider.accounts()`, which runs once before the Run starts, without Inline Fetches; by default it yields those of `sessions.accounts`, a list of tables, then those of `sessions.accounts_file`, a JSON Lines or CSV file read when the Run starts, in which the columns other than `id`, `username`, `password`, and `max_concurrency` are `extra` credentials. A Spider overrides it to import Accounts from elsewhere, such as a database, yielding those of `super().accounts()` to keep them. Accounts without `sessions.enabled` are a `SettingsError`, as are two with one id; a Spider that defines `login()` or `accounts()` without a pool, or Accounts without a `login()`, is warned about.
  - **Leasing a fetch**: a fetch takes its place in its domain slot (§6.3), then leases its Session, then waits for the slot's delay and rate, so it never holds a Session while it waits for a place, which a fetch nested in the one holding the place may need, and holds a place while it waits for a busy Session only for a second at a time, as a fetch nested in the one holding the Session may need the place; the delay, the rate, and a cooldown are checked just before the download, however long the Session took to free up. It holds both until the Response arrives; a streamed Inline Fetch holds them until it is closed. The Request goes down the download chain naming the Session it is sent under, so `response.request.session` and `response.session` name it, and the Fetcher gets the Session as `session=`. The Session is built from the state the pool holds; afterwards, the changes the fetch made are saved as a `StateDelta`, merged per cookie by modification time, removals included, timed as the jar last removed them, whether it held them or not, the localStorage and the client hints of each origin that changed replaced whole, the later save winning, and the Lease is released with the fetch's outcome. A fetch nested in another of the same Session, in the same task, such as one a download Middleware makes, shares its Lease, and an Inline Fetch made there continues that Session; the Lease is released with the most severe outcome of them all, so a 429 a nested fetch got cools the Session down. Callbacks, errbacks, and Events get the Request as it was sent, naming its Session, so an Inline Fetch in an errback continues it too. Rendered Requests lease Sessions as the others do.
  - **Waiting for a Session**: a Request from the Frontier waits a second for a busy Session, then goes back to the Frontier without counting an attempt; it does too while every Session that fits cools down, until the first stops. Inline Fetches wait in place, giving up their place in the slot after each second until the Session frees up. A Request no Session can ever be leased for, as when the one it names is Retired, fails.
- **Health management**:
  - There are three states: available, in Cooldown, and Retired.
  - When Rate Limited (429), a Session only enters Cooldown and is not Retired: until its `Retry-After`, or for `sessions.cooldown` (30 s) without one, at most `sessions.cooldown_max` (300 s). Its domain keeps fetching with the other Sessions (§6.3).
  - Leases carry a token and expire after `sessions.lease_ttl` (300 s) unless renewed, which happens every third of it while their fetch runs; a release or a save from an expired or released Lease is rejected, counted as `sessions.stale`.
  - A Session also cools down with the proxy it follows, and is Retired with it (see Proxies below).
  - Stats: `sessions.created`, `sessions.leased`, `sessions.outcomes.<outcome>`, `sessions.cooldowns`, `sessions.retirements`, every Session Retired, whatever the reason, and `sessions.stale`; logins add their own (see Login below).
  - **Blocked**: `Spider.is_blocked(response)` judges every Response fetched under a Session, but those of a login: by default, whether its status is one of `blocked.statuses`, 403 alone, as 429, Rate Limited, cools the Session down instead, and 401 is for `is_logged_out()`; a Spider overrides it for sites that serve a challenge page instead, such as with status 200. A judgment that raises is logged, and the Response taken not to be Blocked. A Response judged Blocked raises `Blocked`, a `Retry`, is counted as `responses.blocked`, and is not handed to its Callback; the Lease is released as `blocked`, and the Session is Retired after `sessions.max_blocks` (3) in a row, a fetch that went well ending the row. The Request is retried at once under the retry budget `blocked` (§6.4), up to `retry.blocked` (3) times, which take none of its ordinary retries: one naming no Session leases whichever the pool gives next, the least recently leased, so another, with its proxy, and a new one rather than the Session it was Blocked with while the pool has room; one naming its Session, or an Inline Fetch continuing one, is retried with it, until it is Retired. Once the budget is spent, the Request fails with `RetriesExhausted`, naming the budget, and its errback gets it. An Inline Fetch that shares the Lease of the fetch it is nested in raises `Blocked` to that fetch, as `LoginExpired`, which is retried with another Session. A Blocked Response counts against the Session, never against its proxy: as any Response through it, it shows the proxy reachable, ending its row of failures. A Callback that raises `Blocked` itself is retried under the budget too, but its Session's Lease has ended, so it counts toward no Retirement.
  - **Without a Session Pool**, a Response is judged only when its retry can change something: go through another of the Run's proxies, as the next attempt does while another is available, or be rendered (below); otherwise, as without proxies or with a single one, a 403 reaches its Callback, as any other.
  - **Rendered instead**: with `blocked.render`, an HTTP Request found Blocked is retried rendered, as `request.replace(render=True)`, under the same budget, unless it names its Fetcher or is no top-level navigation or form submission (§4.3), which the browser alone loads as a page: the retry policy names the Request to retry (`Decision.retry(..., request=...)`), the Frontier keeps it in place of the one tried (`nack(request=...)`), with its Fingerprint, so it goes to the workers of rendered Requests, and an Inline Fetch replaces it in place. The Request a Decision names keeps the Session and the proxy the one tried was scheduled with, not those it was sent under, so its retry may lease another.
- **Login**: `async def login(self, session, account)` on the Spider logs a Session in; `account` is the Session's Account, or `None` for a Session without one, which can warm up with it, such as to take a guest token. A Spider that defines it has every Session of its pool log in before its first fetch, and again once it is logged out:
  - **Single-flight**: the pool gives a Session that must log in to one fetch at a time, with a Lease whose `must_log_in` is set, and no other Lease on it until the holder reports `logged_in()`; fetches that want the Session wait for it as for a busy one, and lease another meanwhile if they can. The fetch runs the login in its task, holding its place in its slot and the Lease, which the login's Inline Fetches share, so they are sent under the Session, over HTTP or rendered, one after another, and keep the cookies they set; the cookies are saved before the fetch goes on.
  - **Expiry**: `Spider.is_logged_out(response)`, a 401 by default, judges every Response fetched under a Session, but those of the login itself and a streamed one before its body is read. A Response judged logged out raises `LoginExpired`, a `Retry` with no delay, so the Request is retried at once, counting as a retry, on the Frontier's path and in Inline Fetches alike; the Lease is released as `auth_expired`, and the Session logs in again before its next fetch, unless it did since that Lease was given. An Inline Fetch that shares the Lease of the fetch it is nested in, such as one a download Middleware makes, cannot retry under the expired login, so it raises `LoginExpired` to the outer fetch, which is retried once the Session logged in again. A judgment that raises is logged, and the Session taken to be logged in.
  - **Failures**: a login that raises is logged with its traceback, and its Lease released as `login_failed`: the Session cools down for `sessions.cooldown` seconds, while the Request waits for a Session as for a busy one, without counting an attempt, then logs in again. After `sessions.login_attempts` (3) failed logins in a row it is Retired, and the Requests naming it fail with `SessionError`. A fetch logged out right after the login it made counts as a failed login too, so a login that does not work is not repeated without end. A login counts as having worked once the fetch that made it ends with another outcome. A failed login, like an `auth_expired`, changes nothing once the Session logged in again since, as it tells of a login replaced meanwhile.
  - Stats: `sessions.logins` and `sessions.login_failures`.
- **Proxies** (`pernix.proxies`): once a source, or a binding policy, is set, Requests go through proxies. `Request(proxy=...)` goes through its own instead, and an empty one through none, over both the Proxy Pool and its Session.
  - **Sources**: `proxies.urls`, a list; `proxies.file`, a text file of one per line, blank lines and those starting with `#` skipped; `proxies.api`, the URL of an API answering with one per line, as proxy vendors' do, a `Secret` as it often holds a key, fetched with an HTTP Fetcher of its own, never through a proxy, `{count}` in it replaced by how many proxies are wanted, or 1 when that is not known, and the URL as fetched masked in the logs; and `proxies.provider`, a `ProxyProvider` of one's own, whose `async proxies()` gives them, such as from a database, built and opened like other components. JSON is read through `proxies.api_json` (below); another format takes a provider. `proxies.urls` and providers also take `Proxy` records (`pernix.proxies.Proxy`, a msgspec Struct), tables in settings, with a `url`, the `tags` binding policies select a proxy by, such as its region, and `expires`, when it stops working, as its source tells. The proxies of every source are used together, without duplicates, the first given of each kept. They are loaded when the Run starts, a source that fails then failing the Run, and again every `proxies.refresh` seconds if set: those the sources no longer give are no longer used, a refresh that fails is logged and keeps the proxies loaded before, and a proxy listed again keeps its health, a Retired one staying Retired, and takes the tags and the expiry given now. A file with lines that are not proxies is a `SettingsError` naming each; the entries of an API or a provider that are not are skipped with a warning naming their positions. No proxy is quoted in an error or a warning.
  - **Expiry**: a proxy expires once its `expires` passed, as its source tells, or, for those of `proxies.api` whose answer tells none, as one of one per line never does, `proxies.api_lifetime` seconds after the API answered, if set. An expired proxy is used no more: no fetch takes it, and no Session is bound to it; its health is `expired` while Sessions occupy it, so its Sessions are bound as their policy says, with the reason `expired`, and then it leaves the pool, though a Retired one stays Retired should a source give it again. A source giving it again with a later expiry has it available again. A proxy of a Session's own expires too, and a `Proxy` record the policy gives again renews it.
  - **Adding rather than replacing**: with `proxies.supply = "add"`, each load adds the proxies the sources give to those listed, which stay until they expire, or, without an expiry, for the Run, so that with an API answering with new short-lived proxies each time, the Sessions keep theirs; `replace`, the default, lists only what the sources give, as above. A proxy listed again, from any source, is renewed as above.
  - **Extracting on demand**: extraction costs, often per proxy, and vendors limit how often it may be done, so with `proxies.supply = "add"` and `proxies.min_available` set, the API and the provider are asked for more proxies while fewer than that many are available, and when a fetch or a binding finds none: one extraction at a time, `proxies.extract_interval` seconds (1) apart at least, each asking for as many as the level lacks, at least one, and never while a refresh loads the sources. The API is told the count through `{count}`, and a provider whose `proxies()` takes a `wanted` keyword through it; at the other loads, they are told the level; with both, the second is asked for what the first did not give. The proxies they give count against `proxies.max_extracted`, the limit for the Run, as each answers, once reached no more being extracted, at the loads either; an extraction that fails is logged, and after `proxies.extract_attempts` (3) extractions in a row that gave no proxy, none are made on demand, as a vendor out of credit often answers with an error the API cannot tell from an empty answer. While an extraction may still bring proxies, Requests wait for it rather than the Run draining. `proxies.min_available` and `proxies.max_extracted` without `proxies.supply = "add"`, a level without the API or a provider, and `proxies.api_lifetime` without the API are `SettingsError`s.
  - **JSON answers**: `proxies.api_json`, a `pernix.proxies.JsonAnswer` table, has `proxies.api` read its answers in JSON, as extraction APIs often answer, in shapes that differ from one to another; without it, an answer is one proxy per line. Paths are keys joined by dots, such as `data.proxy_list`, a key of digits indexing a list, such as `result.0.items`; `list` is the path of the list of proxies, the answer itself when empty, and each entry's fields are paths within it. An entry that is a string is a proxy's address, as `proxy_url()` takes it; one that is an object is read through `address`, the field holding its whole address, such as `server`, or else through `host` and `port`, with `username` and `password` if set, and `scheme`, `http` by default. `expires` is the field telling when its proxy expires, written as `expires_format` says: `timestamp`, seconds since the epoch, as a number or as text; `timestamp_ms`, milliseconds; `datetime`, an ISO 8601 date and time, such as `2027-01-01 12:00:00`, a space or a `T` between them, with or without an offset; or `seconds`, the seconds left once the answer arrived. A `datetime` written without an offset is in `timezone`, which it needs: an IANA name, such as `Asia/Shanghai`, which needs the system's time zone data, as Windows has only with the `tzdata` package, or a fixed offset, such as `+08:00`; a value's own offset wins. An entry without its expiry expires as `proxies.api_lifetime` says. An answer that does not match, as one that is not JSON, or lacks the list, gives no proxy, with a warning naming the path it lacks, never the API's URL or its key, and counts toward `proxies.extract_attempts`; an entry that lacks a field, or holds one of the wrong kind, is skipped, with a warning naming its position and the field, never a value. A mapping that cannot be read, such as `host` without `port`, both `address` and `host`, or a `datetime` without `timezone`, is a `SettingsError`, and so is `proxies.api_json` without `proxies.api`.
  - **Keys**: a proxy is a URL such as `http://user:password@proxy:8080`, `http` when the scheme is left out, and one of `http`, `https`, `socks4`, `socks4a`, `socks5`, and `socks5h`; its port is required, as curl and Chrome default to different ones. `host:port:user:password`, as some vendors list proxies, is read as `user:password@host:port`, everything after the third colon being the password, and what a URL's credentials cannot hold as it is percent-encoded. A proxy's key is all of it, the scheme and the host lower-cased, so that different sticky sessions on the same gateway, which differ only in their credentials, are not merged into one.
  - **Binding**: a Session goes through the proxy it is bound to, over HTTP and rendered, so all its Requests leave from one address. It is bound as the binding policy picks (below), before the first fetch of each Lease that goes through its proxy, those of its login and those nested in another included; by default it keeps its proxy while that is available, and is otherwise bound to the available proxy with the fewest Sessions, so Sessions bound while enough proxies are available get one each. The binding is saved with the Session's state (`StateDelta.proxy`), and the pool holds it too, so Leases of one Session that overlap go through one proxy. A Session bound to another proxy has its browser contexts replaced, as they fit only their proxy; each Lease settles the proxy once, and its later fetches keep it, so each is reported against the proxy it went through, and a Session whose proxy failed during a Lease is bound to another by the next. A Session the Session Pool Retires is unbound, so it no longer counts toward its proxy's Sessions. A Request with a proxy of its own needs none of the pool's, and does not wait for one. Without a Session Pool, each attempt goes through the available proxy used least recently, so the proxies take turns and a retry goes through another; the Request as sent names it in `proxy`.
  - **Binding policy**: sites differ, some letting an Account change address freely, some letting many Accounts share one, some allowing one address per Account, so how Sessions are bound is a component, `proxies.binding`, as `retry.policy` is, opened with the proxies:
    - Its `async bind(binding)` returns the URL of the proxy to bind, the Session's current one to keep it, whatever its health, a `Proxy` record to give the Session a proxy of its own, or `None` to wait. A `Binding` holds the Session's id, its Account, its current proxy and that proxy's health, the reason for binding (`new`; `lease`, while its proxy is available; `cooldown`; `retired`; `expired`; or `unlisted`), and the candidates: every proxy listed, and the Session's own, least recently used first, each a `ProxyCandidate` with its health, tags, the Sessions bound to it, those leaving it, bound to another since while their Leases still go through it, its failures in a row, its expiry, while it cools down `until` when, and whether it is the Session's `own`. `binding.open` gives those available with room for the Session.
    - The policy is asked at every binding, even while no proxy is available, as it may give the Session a proxy of its own, and once more when a fetch through the Session's proxy failed (below). The pool binds a Session only to a proxy that is available and has room for it, with fewer than `proxies.max_sessions` other Sessions occupying it, those bound to it and those leaving it, in one step. Each Lease holds the proxy it was bound to until it ends, so a Session an overlapping Lease bound to another still counts toward it meanwhile, and with 1 no two Sessions ever go through one proxy at once, overlapping Leases and rebinding included. When another Session took the last room first, the policy is asked again, with the candidates read anew, up to three times.
    - A policy reads health but never changes it, and has no side effects, as it may be asked again for one Lease. One that raises, picks a URL the pool does not list, or gives a proxy of its own that is not valid or that the pool lists, fails the fetch, as a Middleware that raises does.
    - The default policy (`DefaultBindingPolicy`) keeps a Session on its proxy while that is open, and otherwise binds it to the open proxy with the fewest other Sessions, the least recently used of those. `proxies.rebind` tells when it binds a Session to another: `unavailable`, the default, once its proxy cools down, is Retired, expired, or is no longer listed; `every_lease` at each Lease, binding another open proxy if there is one, so a Session's Leases go through the proxies in turn; `retired` only once its proxy is Retired, expired, or no longer listed, keeping the Session on it while it cools down; and `never`, keeping it on its proxy whatever happens, so the Session is Retired with it. `never` while `proxies.refresh` replaces the proxies is warned about when the Run starts, as each refresh then Retires the Sessions whose proxies it no longer lists.
    - **Following its proxy**: a Session the policy keeps on its proxy while that cools down follows it: its Lease is released as `proxy_cooldown`, until the proxy stops cooling down, so it is not leased again only to be given back, and its Requests wait for a Session as for a busy one, without counting an attempt; it stays bound to its proxy, counting toward its `proxies.max_sessions`. Once a fetch through the proxy failed, the policy is asked again, as at the next Lease, and the Lease of a Session it keeps there is released as `proxy_failed` until the proxy stops cooling down; a policy that raises then is logged, and the Session left as it is. Once its proxy is Retired, expired, or no longer listed, a Session the policy keeps on it is Retired with it: its Lease is released as `proxy_retired`, it is unbound, a Request naming no Session leases another, and those naming it fail with `SessionError`, as does a nested fetch that would bind it.
    - **Proxies of a Session's own**: a policy returns a `Proxy` record for a proxy the pool does not list, such as an Account's own read from a database, or a sticky session on a gateway built from the Session's id. It is checked as a source's proxies are, and its password masked in the Run's log; the pool holds it for that Session alone while the Session is bound to it, or its Leases go through it: it takes no turns, it is among the candidates of that Session only, `proxies.max_sessions` does not apply, and refreshes keep it, even should a source list it. Its health is kept as any other's, and the Session follows it as it would a proxy listed, its login's fetches included; once the Session leaves it, another may be given it, with its health, and a proxy no longer listed is given to none while the Sessions bound to it before still go through it. A policy alone, with no source, gives the Sessions of a Session Pool their proxies; without a Session Pool, no Request could have a proxy, so it is a `SettingsError`.
    - When the policy finds no proxy for a Session, as when every proxy available has `proxies.max_sessions` already, or no source is set, the Lease is released and the Request waits a second, as for a busy Session, without counting an attempt; it is counted as `proxies.binding_waits`, and logged once until the Session is bound again. While no proxy listed is available, it waits for one as below.
  - **What a binding does not guarantee**: without `proxies.max_sessions`, bindings are not exclusive, so Sessions share proxies once there are more of them than proxies available, as when some cool down; with it, the Sessions beyond the room the proxies have wait, and the Requests naming one of them wait as long as it finds none. With the default policy, a Session bound to another proxy stays on it, and does not go back once its own stops cooling down. A Session thus changes address, keeping its cookies and its login, whenever its proxy cools down, is Retired, expires, or is no longer listed, or with `retired` once it is Retired, expired, or no longer listed; with `never`, it is Retired rather than change address. A refresh replaces the proxies unless `proxies.supply = "add"`, so with an API that answers with a new batch each time, every Session changes address at each refresh, or with `never` is Retired; with `add`, each keeps its proxy until it expires. A proxy of a Session's own keeps it on one address only as long as the policy gives it. Bindings are held for one Run, and the next binds anew, but for a Session whose state names a proxy not listed, which `never` Retires. Proxies are not released to their vendor, nor extractions shared by Workers.
  - **Health**: a `ProxyError` counts against its proxy: a proxy that cannot be reached, answers 407, or fails a tunnel, and Chrome's proxy errors. A timeout, which a slow site causes as well, does not count, and a Response through the proxy, whatever its status, ends a row of failures. A failure cools the proxy down for `proxies.cooldown` (60 s), doubled with each failure in a row, at most `proxies.cooldown_max` (600 s), and Retires it after `proxies.max_failures` (5) in a row. The fetches that took the proxy before its last failure counted, such as those in flight when it went down, tell of the same outage and change nothing. The Request is retried as a `ProxyError` is (§6.4), through another proxy, and the Lease of its Session is released as `proxy_failed`, which leaves the Session as it is, unless it follows its proxy (above). Only the proxies the pool gave are reported, those of a Session's own included, not a Request's own.
  - **No proxy available**: while every proxy cools down, a Request waits in the Frontier, without counting an attempt, until the first stops, an Inline Fetch waits in place, and a Session leased meanwhile is released. When none ever will be available, as every proxy is Retired or expired or the sources gave none, no refresh that may bring proxies is to come, as one whose sources all extract, once they reached their limit, will not, none can be extracted on demand, and no Session goes through a proxy of its own that is available or cools down, the Run is drained, logging why, so its Requests stay in the Frontier for a later Run to resume rather than becoming Dead Requests; with a refresh to come, they wait for it.
  - **Credentials**: proxy credentials in URLs are masked in the logs (§8), and so is the password of every proxy loaded, or given a Session as its own, wherever it occurs; stats name no proxy, and the `repr` of a Session, a `Proxy`, a `ProxyCandidate`, and a `Binding` shows proxies without them.
  - **Stats**: `proxies.bindings`, `proxies.binding_waits`, `proxies.failures`, `proxies.cooldowns`, `proxies.retirements`, `proxies.expirations`, `proxies.refreshes`, `proxies.refresh_failures`, and, with `proxies.supply = "add"`, `proxies.extractions`, the loads of the API and the provider, `proxies.extracted`, the proxies they gave, and `proxies.extraction_failures`; and, at each snapshot, how many of the proxies listed, and of the Sessions' own in use, but those expired, are `proxies.available`, in `proxies.cooldown`, and `proxies.retired`.
  - The pool is a `ProxyPool`: `replace()` lists the proxies the sources gave, `add()` lists more, `take()` gives one fetch a proxy, `candidates(session)` reads what policies see, `bind(session, proxy)` binds a Session for one Lease if the proxy has room, holding a `Proxy` record as the Session's own, `release()` ends that Lease's hold on it, `unbind()` releases a binding, and `report()` and `ready_in()` keep the health. The in-process `MemoryProxyPool` holds the health of the proxies and the bindings for one Run, and one shared by Workers keeps the protocol (§5.3.1).
- **Two implementations**: `SessionPool` is a protocol. The in-process implementation is the default and suits a single process; the Redis implementation lets multiple Workers, multiple machines, and even multiple Spiders share the same Session Pool, and uses the same Redis as the distributed Frontier (confirmed).

#### 5.3.1 Session Pool shared across Workers (Redis)

```python
class SessionPool(Protocol):
    name: str
    async def acquire(self, *, session: str | None = None, tags: Collection[str] = (),
                      timeout: float | None = None) -> SessionLease | None: ...
    async def ready_in(self, *, session: str | None = None, tags: Collection[str] = ()) -> float: ...
    async def renew(self, lease: SessionLease) -> bool: ...
    async def logged_in(self, lease: SessionLease) -> bool: ...  # the holder of a must_log_in Lease logged in
    async def release(self, lease: SessionLease, outcome: Outcome) -> bool: ...
    async def load_state(self, lease: SessionLease) -> SessionState: ...
    async def save_state(self, lease: SessionLease, delta: StateDelta) -> bool: ...

# Outcome: ok / rate_limited(until) / blocked / auth_expired / login_failed(until) / proxy_failed(until)
#          / proxy_cooldown(until) / proxy_retired / error; any until cools the Session down
```

1. **Atomic leasing**: a single Lua script does "pick an available Session (not in Cooldown, not Retired, concurrency not full) → store the holder and expiry time → issue a token" in one step. Holdings are tracked in a sorted set of "holder → expiry time"; after a Worker crashes, its holdings are reclaimed by expiry time, so capacity never shrinks permanently. The Lease is renewed automatically while the Request is being processed.
2. **Concurrency limit across Workers**: each Session has a `max_concurrency` that applies across all Workers. Sessions bound to an Account default to 1, i.e. exclusive; anonymous Sessions can be set higher.
3. **Preventing stale writes**: both `release` and `save_state` carry the token. When an old Lease that has been judged expired writes back, the write is rejected and does not overwrite the new holder's state.
4. **Health, Cooldown, and quotas**:
   - Cooldown expiry, error score, use count, daily quota, and minimum interval between uses are all updated atomically in Lua, with time taken from the Redis server clock.
   - `rate_limited` puts the Session in Cooldown until the time given by `Retry-After` or the target site's rate-limit headers; it is not Retired.
   - `blocked` adds to the error score, and the Session is Retired once the score exceeds a threshold; the in-process pool counts the Blocked outcomes in a row, up to `sessions.max_blocks`.
   - `auth_expired` marks the Session as needing to log in again, unless it logged in since the Lease was given; `login_failed` cools it down too, and Retires it after too many in a row.
   - `proxy_cooldown`, and `proxy_failed` with an `until`, cool the Session down with the proxy it follows; `proxy_retired` Retires it with its proxy.
   - A Cooldown or Retirement detected by one Worker takes effect immediately for all Workers.
5. **Merging Cookies and storage state**: merging happens at the granularity of individual Cookies (domain, path, name), each carrying a modification time; a Worker writes back only the Cookies it changed and never overwrites the whole set. Browser localStorage is merged per origin.
6. **Single-flight login**: the Lease whose `must_log_in` is set is the login lock, with its TTL and token: the pool gives no other Lease on the Session until its holder reports `logged_in()`, so only one Worker logs in for a given Session while the other Workers wait for the outcome; if the Worker holding it crashes, the Lease expires and another logs the Session in.
7. **Proxies work the same way**: proxies are another kind of resource under the same mechanism, and their health is shared across Workers. A proxy's key includes its username and password.
8. **Namespaces and sharing scope** (confirmed): pools have their own names and are not tied to a `run_id`, so different Spiders and different Runs can share the same Session Pool of Accounts or the same proxy pool; concurrency limits, Cooldowns, and quotas apply to all consumers together. All keys share one hash tag, for compatibility with Redis Cluster.
9. **Security**:
   - Cookies, Tokens, and Account credentials are sensitive data; serialization uses msgspec, not pickle.
   - They are automatically redacted in logs and stats.
   - Optional encryption (confirmed): when a key is configured, data is stored encrypted with AES-GCM; otherwise it is stored in plaintext and a warning is issued at startup.
10. **Management boundary**: the framework provides a minimal management API and CLI (`pernix pool add / list / retire / reset`) for importing Accounts or proxies and for troubleshooting. Visual management, alerting, Account registration, and manual review belong to the Platform.

### 5.4 Basic anti-detection

**Profile consistency**

- HTTP uses a pinned-version Chrome Profile by default (`http.impersonate`, `chrome150`), whose TLS and HTTP/2 are those of the bundled browser, Chrome 151, which changed neither from 150. Aliases such as `chrome`, which drift as the library is upgraded, are rejected, as are unknown targets, with a suggestion.
- The default Profile and the browser build form a Pinned Pair: curl-cffi and patchright are pinned to exact versions, and an upgrade of either lands only with the Profile contract tests passing on macOS and Linux (ADR 0010).
- The Profile contract tests (`tests/test_profile_contract.py`) show that, under one Session, the HTTP Fetcher and the browser Fetcher put one client on the wire. A loopback server records, per connection, the TLS ClientHello, the HTTP/2 preface (the SETTINGS, WINDOW_UPDATE, and PRIORITY frames before the first HEADERS frame), each HEADERS frame with the HPACK representation of each field, and each request's fields in order; HTTP/1.1 requests are recorded as received. Its certificate comes from a CA curl is given, and the browser trusts it through `--ignore-certificate-errors-spki-list`, so that the connections are as valid as any, resumed ones included. Both Fetchers make the same top-level navigations: a first connection, a second request on one connection, and a resumed connection, which the server forces by closing the first with a GOAWAY. Any difference fails, named, such as `connection 1 > HTTP/2 preface` with both sides' frames. Left out, each for its reason:
  - GREASE values, random for each connection; where they are is compared.
  - The ClientHello's random, the bytes of its legacy session id, and the keys of its key shares, all random.
  - The order of TLS extensions, which both clients shuffle for each connection.
  - The config id, the encapsulated key, and the payload of the GREASE `encrypted_client_hello` extension, the payload's length included, all random.
  - The identities, ticket ages, and binders of `pre_shared_key`, which come from the server's ticket; their number and the binders' lengths are compared.
  - The length of the `padding` extension and of the records, which follow from the rest.
  - SETTINGS and PING acknowledgements, sent whenever the server's frames arrive, and the frames after a connection's first HEADERS frame other than HEADERS, such as WINDOW_UPDATE and GOAWAY, which follow the sizes of the answers and how the connection ends.
  - Connections that carry no request, which a browser may open ahead of need.
- One difference is inside curl: once a server announces `SETTINGS_HEADER_TABLE_SIZE`, curl's HPACK encoder starts its next header block with a dynamic table size update, even when the size is unchanged, which Chrome sends only when the size changes, and Chrome grows its table to a larger size announced, where curl keeps 4096. The contract server announces no header table size, as many servers do not, and a test expected to fail records the difference, so that a curl-cffi upgrade removing it shows.
- The headers that name the client come from the bundled browser (`pernix.fetchers.pinned_pair`): with the pinned Profile, the HTTP Fetcher sends `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `User-Agent`, and `Accept-Language` as that build sends them on the platform the Run is on, each in the place of the Profile's own, on every redirect hop, unless the Request sets its own. The build's version and its brands are recorded, as Chrome for Testing names them, `"Chromium";v="151", "Not=A?Brand";v="99"`, so a Run that never renders needs no browser; the User-Agent is Chrome's reduced one for the platform, `macOS`, `Linux`, or `Windows`. Another Profile, from the settings, `Request(impersonate=...)`, or a Session, sends its own headers.
- The browser presents the same: in headless mode, its contexts present its own User-Agent with `HeadlessChrome` turned into `Chrome`, through patchright's `user_agent` option, which reaches every page, frame, and worker without moving the header. On Linux the high-entropy client hints scripts see are the build's own; on macOS and Windows, patchright guesses their architecture and platform version from the User-Agent.
- Both present the languages of the `locale` setting, `["en-US", "en"]` by default, a list or a comma-separated string: the browser through Chrome's `--accept-lang`, unless `browser.launch_options` sets it, and the HTTP Fetcher with the `Accept-Language` Chrome makes of them, each regional language followed by its base language unless the next shares it, weighed 0.1 less each, down to 0.1. Chrome replaces some lists with its own for their first language, such as `de-DE,de` with `de-DE,de,en-US,en`, so the browser Fetcher asks the first browser of a Run which languages its pages present, and warns, naming the list to set, when they differ.
- A Run warns, once each, about a pair other than the pinned one: `http.impersonate` naming another Profile, `browser.launch_options` naming another browser with `executable_path` or `channel`, and a launched browser of another version than the record. `pernix check` marks the Profile and the browser as pinned or not (§4.12).
- Each kind of request (§4.3) has the headers Chrome sends for it, measured on the bundled build (`pernix.fetchers.chrome_headers`): their order, `Accept`, `Sec-Fetch-Mode`, `Sec-Fetch-Dest`, `Sec-Fetch-User` (navigations and forms), `Upgrade-Insecure-Requests` (documents), `Cache-Control: max-age=0` (forms), `Priority`, and the HTTP/2 stream weight made of it, 256 for documents and style sheets, 220 for fetches and scripts, and 183 for images. The page a Request comes from gives the `Referer` sent, by Chrome's default policy, `strict-origin-when-cross-origin`; the `Origin`, for forms and for fetches other than same-origin GETs and HEADs, `null` once a redirect from another origin led to yet another; and `Sec-Fetch-Site`, `none` for a navigation without a page and the furthest relation of the redirect chain otherwise. A request a page makes without a `Referer` comes from a page of its own origin. Loads and frames of another site add `Sec-Fetch-Storage-Access: active`. Over `http://` to an origin that is not potentially trustworthy, a host other than a loopback one, Chrome sends no client hints and no `Sec-Fetch-*`, and `Accept-Encoding: gzip, deflate`; `http://localhost` gets the whole set.
- The contract tests (`tests/test_profile_contract_kinds.py`) load a page that makes each kind of request in the browser, and send the page's request and a Request of that kind over HTTP, and compare both: links to the same origin and to another site, iframes of both, a form submission, fetches to both, a POST of JSON, a style sheet, a script, an image, cleartext to a loopback host, HTTP/1.1, and an insecure origin, which both map to the server. Known differences, recorded by tests expected to fail or left out:
  - curl's HPACK encoder never adds `content-length` to the dynamic table, which Chrome's does, so the HPACK representations of a request with a body differ from the field on; their headers are compared, their HPACK left out.
  - Chrome orders the headers a page sets among its own by a hash of their names, so a Request's own new headers go first among the page's instead.
  - Chrome raises an image's priority to `u=1, i` when its layout finds it in the viewport before the request starts, and lowers a small image's to `i`; the HTTP Fetcher sends an image's default, `u=2, i`, as Chrome does for one out of view. Scripts and style sheets also vary with where a page puts them.
  - Chrome makes each concurrent HTTP/2 stream depend on the one before; curl's depend on none. It also pools HTTP/2 connections across hosts sharing an address and a certificate, and sends a CORS preflight before a fetch to another origin with headers of its own; curl does neither.
- Client hints (`pernix.fetchers.client_hints`): an origin asks for client hints with the `Accept-CH` of the response to a top-level navigation or form submission, its redirects included, over a potentially trustworthy origin, as Chrome takes them; the responses of iframes, fetches, and loads ask for none.
  - `Accept-CH` is read as Chrome reads it: a Structured Field list of tokens, which may have parameters, each naming a hint in any case, those Chrome does not know skipped. The hints it names replace those the origin (scheme, host, and port) asked for before; an empty one, or one naming no hint Chrome knows, forgets them; a response without one, or with one that is not a list of tokens, changes nothing. Chrome keeps them with its profile, with no expiry, so they are kept in the Session, with its state (§5.3); a fetch without a Session keeps them within its own redirect chain only (ADR 0011).
  - With the pinned Profile, the hints an origin asked for go out as Chrome sends them: with each navigation and form submission to it, and with the iframes and the other requests of its pages to it, a page being the one the Request's `Referer` names; another origin gets only `sec-ch-ua`, `sec-ch-ua-mobile`, and `sec-ch-ua-platform`, which every potentially trustworthy origin gets. In a navigation, a form submission, or an iframe's, they go in Chrome's order in the place of those three; a redirect adds those of the origin it leads to after `Sec-Fetch-*`, and never takes one away, so a navigation's hints stay on when it is redirected to another origin, as Chrome's do; a `fetch()` that has gone to another origin than its page's sends the three alone.
  - The values are those the browser's pages present in headless mode: the high-entropy hints of the User-Agent as patchright makes them of it (architecture `x86`, bitness `64`, an empty model, the platform version `10_15_7` on macOS, empty on Linux, and `10.0` on Windows), the bundled build's full version, the pages' viewport of 1280 by 720, a device pixel ratio of 1, the light color scheme and no preference for reduced motion or transparency, and the machine's memory, as Chrome rounds it, to the nearest power of two from 2 to 32 gigabytes. The network hints tell of a fast connection, an RTT of 50 ms and a downlink of 10 Mbit/s, with Chrome's noise for each host, from a salt of each HTTP Fetcher, and `4g`. The width of an image, which Chrome sends only for those its page lays out, and `Save-Data` are never sent.
  - `Critical-CH`: a navigation (a GET) whose response's `Critical-CH` names a hint its `Accept-CH` asks for that its origin had not asked for, and that the first request of the navigation did not send, is sent again from the Request's URL, as Chrome restarts it, once for each origin: every hint then goes after `Accept`. The response dropped is no redirect and is not in `Response.history`, the cookies it set are kept, and the deadline of the fetch covers both. Form submissions, iframes, and the other kinds are never sent again. Another Profile sends no hint and sends nothing again, but the hints asked for are kept all the same.
  - The browser keeps the hints its pages' navigations ask for in its context, as Chrome does, and they are kept in the Session after each fetch (§5.3). Chrome offers no other way to set them, so before a fetch a context is taught those of each of the Session's origins that it does not keep with a navigation that a route answers in the browser itself, past any service worker of the origin, with an `Accept-CH` naming them, or an empty one to forget them (`pernix.fetchers.browser_hints`); one that something else answered fails, is logged, and is tried again before the next fetch. The request never leaves the browser, but Chrome connects to the origin ahead of the navigation, as for any: one TLS handshake, which the next navigation there may use.
- The contract tests for client hints and cookies (`tests/test_profile_contract_hints.py`) give each Fetcher a Session of its own, so that what the HTTP Fetcher kept does not reach the browser: a navigation after `Accept-CH`, a form submission, iframes of the same origin and of another site, a link and a fetch to another site, navigations sent again for `Critical-CH`, before and after a redirect, a redirect asking for hints, a redirect to another site, an empty `Accept-CH`, the network hints, a Session's hints from its first request, and a Session's cookies of several paths, over HTTP/2 and HTTP/1.1. Known differences, recorded by tests expected to fail or left out:
  - Chrome orders the hints a page's requests send by a hash of their names, as it orders a page's own headers; the HTTP Fetcher puts them after `sec-ch-ua-mobile`, in the order of a navigation.
  - The values of the network hints follow Chrome's estimate of the connection, with noise from a salt of each run; where they go is compared, their values are not.
  - curl's HPACK encoder never adds a `cookie` field shorter than 20 bytes to the dynamic table, which Chrome's does, so the HPACK representations of a request with a short cookie differ from that field on, and those of the requests after it on its connection; their headers are compared, and their HPACK only for longer cookies.
  - Chrome sends the hints a page delegates to another origin with `Permissions-Policy`, or that a `<meta http-equiv="Accept-CH">` asks for, to its requests; the HTTP Fetcher, which never sees the page, does not.
  - On macOS, Chrome gives the hints a redirect adds the color scheme of the system, which may be dark, rather than the page's.
  - With a window, `browser.headless = false`, the browser presents its own high-entropy hints, such as `arm` on a Mac with Apple silicon, and the HTTP Fetcher still those of headless mode.
- By default no extra browser characteristics are injected or randomly generated, to avoid contradicting the real TLS/HTTP2 characteristics.

**Pacing**

- Concurrency and delay (with jitter) are set per domain, `Retry-After` is honored, and a `429`/`503` with `Retry-After` makes the whole domain back off together (§6.3).
- Concurrency slots are computed along two dimensions, domain and Session: a fetch takes its domain's slot, whose concurrency, delay, rate, and cooldown apply to all Sessions together, and a Lease on its Session, within the Session's `max_concurrency`; a 429 cools only its Session down (§5.3).

**Handling Blocked responses**

- Blocked-response detection: the status codes of `blocked.statuses` (403), plus an overridable `Spider.is_blocked(response)`.
- Once a Response is identified as Blocked: retry with a different Session or proxy, under a retry budget of its own; with `blocked.render`, an HTTP Request found Blocked is retried rendered (§5.3).

**Browser side**: see the defaults in §5.2. The following must be verified by real-world testing before going live:

- the actual values of the UA and `sec-ch-ua` in headless mode;
- the egress actually in effect for proxies set per context;
- cancellation behavior.

## 6. Scheduling layer

### 6.1 Frontier

Interface semantics:

- `add`: atomically performs "deduplicate + insert".
- `claim`: claims a Request and obtains a Lease with a token; `claim(rendered=True)` claims only the Requests that go to the browser Fetcher, and `rendered=False` only the others.
- `renew`: renews the Lease.
- `ack`: acknowledges completion.
- `nack(delay)`: returns the Request with a delay, optionally counting it as a failure; `nack(budget=name)` counts it as a retry under a retry budget instead (§6.4), in `Lease.spent`; `nack(request=...)` keeps another Request in its place, as a retry policy may have one retried rendered, with the Fingerprint and the slot of the one returned, and the kind of workers the new one needs.
- `dead`: marks the Request as permanently failed; it is kept as a Dead Request.

Rules:

- All write operations are conditional on `(id, token)`; writes from a stale old Lease have no effect.
- Lease deadlines are taken from the backend clock.
- A Lease carries the item id, a token that increases with every claim, the attempt number (from 1; `nack(count_attempt=False)` does not count, an expired Lease does), the expiry time, the concurrency slot (the host by default, used by `claim(skip_slots=...)`), whether the Request is rendered (used by `claim(rendered=...)`), the retries it had under each retry budget (`spent`), and the Request.
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
  - It never includes cookies, whether passed as `cookies=` or as a `Cookie` header; declaring the `Cookie` header for the Fingerprint is rejected. By default it does not include the fetcher, the Session, or the Render options either, so the same URL is not fetched again just because it was switched to Render, rendered with other options, or carries different cookies; fetching it again is the application's explicit decision, through `dont_filter` or a Fingerprint it supplies.
  - The Fingerprint function can be replaced with `fingerprint.function`, a callable `(request) -> str` that may fall back to `request_fingerprint()`, or the application can pass in a Fingerprint directly with `Request(fingerprint=...)`.
  - `dont_filter` bypasses deduplication: such a Request is neither checked nor recorded.
- URL normalization does only three things: serialize according to WHATWG rules, remove the fragment, and stably sort query parameters by name (`fingerprint.sort_query`, on by default).
  - It does not change path case, remove trailing slashes, or remove tracking parameters; these can be enabled with optional rules when needed.
  - Percent-encoded bytes are preserved as is.

### 6.3 Concurrency, rate limiting, and robots

- **Concurrency**:
  - Global default 16 (`concurrency`, the number of workers) for the Requests that do not go to the browser Fetcher, and `browser.max_pages` (8) workers more for those that do, as `pernix.fetchers.is_rendered()` tells when they are added to the Frontier: those naming the `browser` Fetcher, or naming none and needing rendering. Per-domain default 8 (`domain.concurrency`).
  - Each worker claims only Requests of its kind (`claim(rendered=...)`), so HTTP Requests never wait for the browser, nor rendered ones for HTTP. Both kinds share their domain's slot, delay, rate, and cooldowns, so a site sees one budget whichever way it is fetched.
  - Two kinds of rendered fetch wait for a page in the worker they run in, which may be one for the other Requests: a Request a download Middleware switches to Render, claimed as it was added, and a rendered Inline Fetch, in the worker of its Callback.
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
- `retry(delay, reason, budget=None, request=None)`: the Request is fetched again after the delay, and `RequestRetried` is emitted; with `request`, that Request in its place, such as the same rendered, which keeps the Session and the proxy the one tried was scheduled with. With a `RetryBudget(name, limit)`, the retry counts against that budget instead of as an attempt: the `attempt` the policy is next told of is the same, and once the Request had `limit` retries under the budget, the next one fails it with `RetriesExhausted`, naming the budget.
- `give_up(reason)`: a retry was called for but none is left; the Request fails with `RetriesExhausted`.

Default policy (`DefaultRetryPolicy`, configured by `retry.*`):

- At most 3 retries (`retry.times`; 0 turns retrying off).
- Retried status codes (`retry.statuses`): 408, 429, 500, 502, 503, 504, 522, 524. Retried exceptions: `Timeout`, `ConnectError`, `ProxyError`, `PageCrashed`, and `Retry`, including `LoginExpired` for a Session found logged out (§5.3); other `FetchError`s, such as `TLSError` or `TooManyRedirects`, would fail again.
- `BrowserLost` is retried at once under the `browser_lost` budget, up to `retry.browser_lost` (3) times, which take none of `retry.times`: the fetch did not fail by itself, but with every fetch in its browser.
- `Blocked` is retried at once under the `blocked` budget, up to `retry.blocked` (3) times, which take none of `retry.times`, with another Session or through another proxy, and, with `blocked.render`, rendered, the Decision naming `request.replace(render=True)`, unless it is no top-level navigation or form submission (§5.3). A custom policy that does not tell `Blocked` retries it as the `Retry` it is.
- The delay is the one `Retry` names; otherwise a `Retry-After` header's, in either format, up to 300 s (`retry.retry_after_max`); otherwise full-jitter backoff with a 1 s base (`retry.backoff_base`) doubling per retry, up to 60 s (`retry.backoff_max`). `retry.delay_scale` multiplies every delay; tests set it to 0.
- `retry.policy` replaces the policy with any object that has `decide()`; a class gets `RetrySettings` injected. A policy that raises keeps the outcome.

Rules:

- For Requests that go through the Frontier, a retry returns the Request with `nack(delay)`, so it holds no concurrency slot while it waits, and every Middleware runs again; Inline Fetches are retried in place.
- A Request whose retries ran out, or that failed in a way the policy keeps, is kept as a Dead Request, `RequestFailed` is emitted, and its errback runs.
- A Callback's exceptions are not judged by the policy (ADR 0006), but for `Retry`, and for a Callback that keeps its page (§4.7) and fails while that page crashed or its browser was lost: it is judged as `PageCrashed` or `BrowserLost`, caused by the Callback's own error, so the default policy retries it as a crash, or under the `browser_lost` budget. The Items it yielded before are kept, as for `Retry`.
- `CancelledError` is never caught: when a Run is cancelled, Requests in flight are returned to the Frontier without counting an attempt, never retried.

**Retry budgets**: failures that are not the Request's own, such as a browser that went away, or a Session found Blocked (§5.3), are retried under budgets of their own, so they take no ordinary retry, while a Request that keeps meeting them still ends. A policy names the budget and its limit in the Decision, and stays stateless; the Frontier counts the retries of each budget with the Request, as it counts its attempts (`nack(budget=...)`, `Lease.spent`), so the count survives a restart and is shared by Workers; the Engine gives up once the count reaches the limit, and an Inline Fetch counts them in place, for its own retries. `RequestRetried.budget` names the budget, and `requests.retried_by_budget.<budget>` counts its retries, besides `requests.retried`. A custom policy that does not name a budget retries as it always did.

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
- Stopping covers the whole Run, from importing the Spider's Accounts (§5.3) and opening the components to closing them. A Drain requested while the Accounts are imported or the components open applies once the work starts, and its grace period runs from the request. A Force Stop while the Accounts are imported cancels the import, and one while the components open cancels the opening and closes the components already open; the Run ends as `forced` without `SpiderOpened` or `SpiderClosed`, as when opening fails. A Force Stop while they close, including one a Drain turns into after its grace period, limits the close in progress and the rest to `shutdown.close_timeout`.
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
| `requests.retried`, `requests.retried_by_budget.<budget>`, `requests.ignored` | Attempts retried, and those of them under a retry budget, by its name (§6.4); Requests ignored |
| `requests.failed`, `requests.failed_by_error.<ErrorType>` | Requests that failed for good |
| `downloads.responses`, `downloads.by_status.<code>`, `downloads.bytes` | Responses fetched, including those retried afterwards and those of Inline Fetches, and their body bytes, streamed ones as they are read |
| `downloads.errors`, `downloads.errors_by_type.<ErrorType>` | Exceptions fetching raised, other than `IgnoreRequest` and `Retry` |
| `responses.received` | Responses handed to Callbacks |
| `responses.blocked` | Responses judged Blocked, retried with another Session, through another proxy, or rendered (§5.3) |
| `items.scraped`, `items.dropped`, `items.errors`, `items.retried`, `items.failures_lost`, `items.discarded` | Items stored, dropped, or failed; Pipeline calls retried; failed Items a failure Pipeline lost; Items a stopped Run never processed |
| `frontier.ready`, `frontier.delayed`, `frontier.leased`, `frontier.done`, `frontier.dead` | The Frontier at the snapshot |
| `throttle.waits`, `throttle.wait_seconds`, `throttle.cooldowns`, `throttle.refused` | Waiting for per-domain slots, and cooldowns |
| `loop.lag_avg`, `loop.lag_max` | Event loop lag in seconds, sampled every second |
| `log.warning`, `log.error`, `log.critical`, `log.dropped` | Records logged per level, and records dropped from the full queue |
| `sessions.*`, `proxies.*`, `browser.*` | Sessions and proxies (§5.3), and the browser's pages and contexts (§5.2) |

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
- **Responses**: `make_response(url, body, status=, headers=, json=, request=, encoding=, browser=)` builds a Response as a Fetcher would: a `str` body is UTF-8 HTML unless the headers say otherwise, `json=` makes a JSON body, and `request` (by default `Request(url)`) carries the Callback, `kwargs`, and `meta`. A Response to a rendered Request, or with `browser=RenderResult(...)` given, whose Request then renders by default, is the browser Fetcher's: its `fetcher` is `browser`, and `response.browser` is empty unless given.
- **Running**: `crawl(Spider, fetcher=..., browser=..., settings=..., **args)` runs a Spider to completion and returns a `CrawlReport` with the Items that passed every Pipeline, the final stats, the reason, and the `run_id`. `fetcher` stands in for the HTTP Fetcher only, so rendered Requests still go to the browser, on loopback hosts under the network guard; `browser`, such as a `FakeFetcher`, or the `fetcher` itself, answers them instead, without starting a browser, as many at once as `browser.max_pages` says, and its Responses that have no `browser` get an empty `RenderResult`. It runs without periodic stats, retry delays, or per-domain delay, rate, and cooldowns, as those only make a test wait, and with `network.public_only` off, so that a `LoopbackServer` can be reached; `settings` override that. `run_callback(spider, response)` runs one Callback as a Run would, the Request's or `parse`, with its `kwargs`, resolving relative URLs against the Response, and returns its outputs; spider Middleware do not run, and Inline Fetches need `crawl()`.
- **Local server**: `LoopbackServer` serves on 127.0.0.1: routes registered with `route()` or given up front (a body, a `Reply`, or a handler of the `ReceivedRequest`), then the files of a `directory`. It records every request with its headers in wire order.
- **Network guard**: while `blocking_network()` is in effect, the Fetchers reach only loopback hosts (loopback addresses, `localhost`, `*.localhost`), or any host through a proxy on one; others raise `NetworkBlocked`, a `FetchError`, and are recorded in the guard. A host is read as the HTTP Fetcher gives it to curl (`pernix.urls.url_host()`), so `localhost` in full-width letters, or as `local%68ost`, is a loopback host too. The HTTP Fetcher checks each hop of a redirect chain before it is sent, the first included, at the URL it is sent to: a redirect's `Location` resolved as WHATWG URLs resolve it, as browsers go. A browser launched while the guard is on sends everything but loopback traffic to a proxy that refuses it, set at launch so that the browser's own requests, which belong to no page, are stopped too, and WebRTC is kept off UDP, which would go around it; a Request's loopback proxy takes its context's requests instead. With `network.public_only` on too, the proxy that checks hosts for it (§5.2) takes everything, loopback traffic included, and reaches only the hosts both guards admit. The requests its pages make to other hosts, redirects and WebSockets included, are recorded, the browser's own are not, and a refused document raises `NetworkBlocked`. A browser launched while the network was open refuses to render under the guard. This layer is necessary: curl-cffi and the browser do not go through Python's socket, so the usual network-blocking plugins cannot stop them.
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
- **Core dependencies**: curl-cffi (pinned, ADR 0010), parsel (lxml), msgspec, orjson, protego, charset-normalizer, and a WHATWG URL parsing library.
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
