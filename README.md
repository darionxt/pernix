# Pernix

An asyncio crawling framework for Python. HTTP (curl-cffi) and the browser (patchright) are peer ways of fetching, selectable per request. The kernel is minimal; retries, deduplication, rate limiting, sessions, and output are replaceable components, extended through Middleware and Events.

> Status: pre-alpha. The API is not stable yet; the HTTP crawl loop and rendering in the browser, with declarative page actions, are implemented so far.

## Installation

Pernix is not published yet. Install it from a checkout:

```bash
uv add git+https://github.com/darionxt/pernix
```

Requires Python 3.13 or newer. Rendering pages in the browser needs the `browser` extra and the browser engine:

```bash
uv add "pernix[browser] @ git+https://github.com/darionxt/pernix"
uv run pernix browser install   # Chrome for Testing, which the browser Fetcher launches
```

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

## Fetching inside a Callback

```python
class Account(Spider):
    start_urls = ["https://example.com/"]

    async def parse(self, response: Response):
        login = await self.fetch(Request("/login", data={"user": "me", "password": "..."}))  # a POST
        page = 1
        while page:  # page through an API in order
            data = (await self.fetch(Request("/api/orders", params={"page": page}, cookies=login.cookies))).json()
            for order in data["orders"]:
                yield order
            page = data["next"]
        async with self.stream("/export.csv", max_bytes=0) as export:  # read as it arrives
            await export.save("export.csv")
```

`await self.fetch()` is an Inline Fetch: the Request goes through the download Middleware and the per-domain limits, and is retried in place, but it skips the Frontier, so it is neither deduplicated nor stored, and no Callback runs for it. Once its retries run out it raises `RetriesExhausted`. It sends only the cookies you pass, unless the Request being handled has a Session. `self.stream()` reads a body as it arrives: a slow reader keeps about 1 MiB of it in memory, and the transfer holds its domain's slot until the block ends. A Force Stop cancels Inline Fetches with the Request they belong to. See [`docs/design.md`](docs/design.md) §4.6.

`callbacks.timeout` limits how long a Callback, with the spider Middleware, may take to produce its outputs, Inline Fetches included, but not the time its outputs wait to be handed on, such as while the Item queue is full; an errback gets as long for its own. Once the time runs out, the Callback is cancelled at the `await` it is in and fails with `CallbackTimeout`, which its errback gets, and the Request is not retried; a page it kept is closed. The limit is off by default, as some Callbacks take long for good reasons. Code that never awaits cannot be cut short, as Logs and stats below explains. See [`docs/design.md`](docs/design.md) §4.5.

## Requests as the browser makes them

With the pinned Profile, an HTTP Request carries the headers Chrome sends for the kind of request it is, in Chrome's order, with its `Priority` and HTTP/2 stream weight. Its method and body tell the kind, or a `Sec-Fetch-Dest` header names it; its `Referer` header names the page it comes from.

```python
yield Request(url)                                            # a link: a top-level navigation
yield Request(url, data={"q": "dune"})                        # a form submission
yield Request(api, json={"page": 2}, headers={"Referer": response.url})        # a fetch() from the page
yield Request(api, headers={"Sec-Fetch-Dest": "empty", "Referer": response.url})  # a GET by fetch() or XMLHttpRequest
yield Request(frame, headers={"Sec-Fetch-Dest": "iframe", "Referer": response.url})
yield Request(image, headers={"Sec-Fetch-Dest": "image", "Referer": response.url})  # or style, script
```

| Kind | Told by | Sends |
| --- | --- | --- |
| Top-level navigation | a GET, or `Sec-Fetch-Dest: document` | `Upgrade-Insecure-Requests`, `Sec-Fetch-User: ?1`, the HTML `Accept` |
| Form submission | a POST of `application/x-www-form-urlencoded`, `multipart/form-data`, or `text/plain` | also `Origin` and `Cache-Control: max-age=0` |
| iframe | `Sec-Fetch-Dest: iframe` | as a navigation, without `Sec-Fetch-User` |
| `fetch()` or `XMLHttpRequest` | any other method or body, or `Sec-Fetch-Dest: empty` | `Accept: */*`, `Sec-Fetch-Mode: cors`, `Origin` but to its own origin by GET |
| Style sheet, script, image | `Sec-Fetch-Dest: style`, `script`, or `image` | their `Accept`, `Sec-Fetch-Mode: no-cors` |

The `Referer` sent is the page's, by Chrome's default policy: whole to its own origin, its origin alone to another, and none from HTTPS to plain HTTP; `Sec-Fetch-Site` tells how the page and the URL relate, `none` for a navigation without a page. An empty `Referer` names no page. With `http.inherit_referer`, a Request a Callback constructs without a `Referer` takes the page its Response belongs to: that Response's URL when it is a page, or the page its own Request came from, so API calls keep the page that started them. A header a Request sets replaces the kind's of its name in place; another goes after them in a navigation, and first among those the page adds in the other kinds. A `fetch()` to another origin than its page's sends no Session cookies, as `fetch()` does by default. To a plain `http://` host other than a loopback one, Chrome's reduced set goes: no client hints, no `Sec-Fetch-*`, and `Accept-Encoding: gzip, deflate`. Other Profiles send a Request's headers as they are.

The browser renders top-level navigations only, as typed in: a rendered Request's `Referer` is not sent. The contract tests compare each kind with what a page makes Chrome send, and `docs/design.md` §5.4 lists what still differs, such as the order of headers a page sets itself.

## Rendering in the browser

```python
class Shop(Spider):
    async def start(self):
        yield Request("https://shop.example/", render=True)  # loaded in the browser, its scripts run

    async def parse(self, response: Response):
        for href in response.css("#books a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)  # fetched over HTTP

    async def parse_book(self, response: Response):
        yield {"title": response.css("h1::text").get()}
```

`render=True` sends a Request to the browser Fetcher, which loads the page in Chrome until its `load` event; the Response's body is the page as its scripts left it. Rendered and HTTP Requests mix freely in one Spider. The browser starts with the first rendered Request, so a Run that renders nothing never starts it. Each rendered Request gets a fresh browser context, carrying only the cookies the Request passes, as over HTTP. See [`docs/design.md`](docs/design.md) §5.2.

Rendered Requests have workers of their own: `browser.max_pages` (8) of them, as many as the pages the browser opens at once, while the `concurrency` (16) workers take the other Requests, so neither waits for the other. Both kinds share their domain's limits: `domain.concurrency`, the delay, the rate, and cooldowns. A Request a download Middleware switches to Render, and a rendered Inline Fetch, wait for a page in the worker they run in. The `browser.*` stats count the pages and contexts opened, how many are open, and the fetches that waited for a page. See [`docs/design.md`](docs/design.md) §6.3.

Long Runs replace the browser as it ages: after `browser.generation_age` (an hour), `browser.generation_pages` (1000 pages), or, if set, once its processes take more than `browser.generation_memory` MiB; the pages in it finish there while the next go to a new one. A page that crashes fails with `PageCrashed`, retried as other transient errors are. A browser that disconnects, whose driver dies, or that stops answering its health probe fails every fetch in it with `BrowserLost`, retried at once, up to `retry.browser_lost` (3) times, which take none of `retry.times`; the next fetch starts another browser, and the processes of the old one are killed. See [`docs/design.md`](docs/design.md) §5.2.

```python
from pernix.browser import Click, Evaluate, Render, Scroll, WaitFor

yield Request(
    "https://shop.example/books",
    render=Render(
        wait_for="div.item",                              # an element to wait for once the page loaded
        actions=[Click("#accept", optional=True, timeout=2), Scroll(times=3), WaitFor("div.loaded"),
                 Evaluate("() => window.__DATA__")],
        block=("image", "media", "font"),                 # resource types the page may not load
        capture="**/api/list*",                           # XHR and fetch responses to keep
        screenshot=True,
    ),
)
```

`Render(...)` options are persisted with the Request, so a download Middleware can upgrade a Request with `request.replace(render=Render(...))`. After the page loaded, the browser waits for `wait_for`, runs the actions in order, and takes the screenshot; each step may take `browser.action_timeout` (10 s), or its own `timeout`. A step that takes too long raises `ActionTimeout`, which is retried as a slow page is, and one that fails otherwise raises `ActionError`, which is not; an `optional` action that fails is logged, and the next one runs. `response.browser` holds the captured responses, the screenshots, the results of `Evaluate`, which runs in the page unless `isolated=True`, and the document as it was received. `browser.block` lists the resource types no rendered page loads, none by default; `Render(block=...)` replaces it for one Request, and `Render(block=())` blocks nothing. Blocking `stylesheet` or `script` can keep a page from rendering as it should. See [`docs/design.md`](docs/design.md) §4.7.

```python
from pernix.browser import PageMethod, Render

class Shop(Spider):
    async def start(self):
        yield Request("https://shop.example/", render=Render(setup=self.log_in, keep_page=True))

    async def log_in(self, page):                       # before the page loads
        await page.context.add_cookies([...])

    async def parse(self, response: Response):
        await response.page.click("button.more")        # the page itself, open while the Callback runs
        yield {"titles": await response.page.locator("h2").all_text_contents()}
```

Page methods run any patchright code on the page: `setup` before it loads, and `action` once `wait_for` and the actions are done, before the screenshot; `PageMethod(method, optional=True, timeout=5)` lets one fail, logged, or limits its time. Like Callbacks, they are Spider methods, persisted by name, and they run in rendered Inline Fetches too. With `keep_page=True`, the Callback gets the page as `response.page`; the Run closes it, with its context, once the Callback ends, however it ends. It counts toward `browser.max_pages` meanwhile, and the rendered fetches the Callback makes share its slot. Only the Callback of a Request the Run fetches can keep a page: an Inline Fetch raises `ValueError`. A Callback that fails as its kept page crashed, or its browser was lost, is retried as the fetch would be.

HTTP and the browser present one client: with the pinned Profile, HTTP requests carry the `User-Agent` and client hints of the bundled Chrome for Testing build on the platform the Run is on, and the browser, in headless mode, drops `HeadlessChrome` from its own User-Agent, as it is with a window. Both present the languages of `locale`. A Run warns about a Profile or a browser other than the Pinned Pair, and `pernix check` tells whether they are. See [ADR 0010](docs/adr/0010-consistency-over-synthetic-fingerprints.md).

## Sessions

```python
class Account(Spider):
    start_urls = ["https://example.com/"]
    settings = {"sessions.enabled": True, "sessions.size": 4}

    async def parse(self, response: Response):
        yield Request("/login", data={"user": "me"}, session="me", callback=self.parse_home)

    async def parse_home(self, response: Response):
        yield Request("/orders", session=response.session)  # the login's cookies come along
```

Requests are independent by default: each sends only the cookies it carries. With `sessions.enabled`, every Request leases a Session from the Run's Session Pool instead, and sends the Session's cookies, which keep those set during its fetches. Sessions are created as Requests need them, up to `sessions.size`, and each takes `sessions.max_concurrency` fetches at once. `Request(session="me")` leases the Session of that id, created on first use, and `session=response.session` continues the Session of a page; a Request a Callback yields without one leases whichever Session is free. Inline Fetches continue the Session of the Request being handled. A 429 cools only its Session down, so the others keep fetching from the domain. Rendered Requests lease Sessions too: each Session has browser contexts of its own, which hold its cookies and localStorage, so a login in the browser carries over to HTTP, and the other way round. See [`docs/design.md`](docs/design.md) §5.3.

### Logging in with Accounts

```python
class Shop(Spider):
    start_urls = ["https://shop.example/orders"]
    settings = {"sessions.enabled": True, "sessions.accounts_file": "accounts.csv"}  # id,username,password

    async def login(self, session: Session, account: Account | None):
        data = {"user": account.username, "password": account.password.reveal()}
        page = await self.fetch(Request("https://shop.example/login", data=data))
        if page.css(".error"):
            raise RuntimeError(f"cannot log in as {account.id}")

    def is_logged_out(self, response: Response) -> bool:
        return "/login" in response.url  # the site sends logged-out visitors to its login page
```

With Accounts, the pool holds one Session per Account, under its id, and no other; each takes one fetch at a time, unless the Account's `max_concurrency` says otherwise. Accounts come from `sessions.accounts`, tables with a `username`, a `password`, and optionally an `id`, `extra` credentials, and `max_concurrency`; from `sessions.accounts_file`, a JSON Lines or CSV file; and from `Spider.accounts()`, which yields both, and which a Spider overrides to import Accounts from elsewhere, such as a database. Passwords and `extra` credentials are `Secret`s, masked in the logs.

`login()` runs before a Session is first used, in the fetch that leases it, and fetches that want the same Session wait for it to finish, so each Session logs in once at a time; the Inline Fetches it makes, over HTTP or rendered, are sent under the Session. Sessions without an Account log in too, with `account=None`. A Response that `is_logged_out()`, a 401 by default, has its Session log in again, and the Request is retried at once, counting as a retry. A login that raises cools its Session down for `sessions.cooldown` and is tried again; after `sessions.login_attempts` failures in a row the Session is Retired. See [`docs/design.md`](docs/design.md) §5.3.

```python
class Shop(Spider):
    settings = {"sessions.enabled": True, "blocked.render": True}

    def is_blocked(self, response):
        return response.status == 403 or bool(response.css("#challenge"))  # a challenge page, served with 200
```

A Response in which the site refuses its Session, Blocked, a 403 by default or whatever `is_blocked()` says, is not handed to its Callback: the Request is retried at once with another Session, and its proxy, under a budget of its own, `retry.blocked` (3), which takes none of its ordinary retries, and the Session is Retired after `sessions.max_blocks` (3) Blocked Responses in a row. Without a Session Pool, a Response is judged only when the retry can go through another proxy, as one is available; with `blocked.render`, an HTTP navigation found Blocked is retried rendered, with the browser. Once the budget is spent, the Request fails, and its errback gets `RetriesExhausted`. A 429 is not Blocked: it cools its Session down. See [`docs/design.md`](docs/design.md) §5.3.

## Proxies

```python
class Shop(Spider):
    start_urls = ["https://shop.example/"]
    settings = {
        "proxies.urls": [
            "http://user-session-a1:pw@gate.example:7777",
            {"url": "http://user-session-b2:pw@gate.example:7777", "tags": ["us"]},
        ],
        "proxies.api": "https://vendor.example/proxies?key=...",  # one per line, fetched again every refresh
        "proxies.refresh": 300,
        "proxies.max_sessions": 1,  # no two Sessions share a proxy
    }
```

Once a source is set, Requests go through proxies: those of `proxies.urls`, of `proxies.file`, a text file of one per line, of `proxies.api`, a URL answering with one per line, and of `proxies.provider`, a `ProxyProvider` of your own, such as one reading a database, all used together. The file, the API, and the provider are read again every `proxies.refresh` seconds, if set. A proxy is a URL with its port, `http` when the scheme is left out, or `host:port:user:password`, and all of it tells proxies apart, credentials included, so that sticky sessions on one gateway stay apart. `proxies.urls` and providers also take `Proxy` records, tables in settings, whose `tags` binding policies select them by, and whose `expires` tells when they stop working: an expired proxy is used no more, and its Sessions are bound to others.

Without a Session Pool, each attempt goes through the next proxy in turn, so a retry goes through another. A Session is bound to one proxy, which its HTTP and rendered Requests all go through while it is available; Sessions bound while enough proxies are available get one each, but bindings are not exclusive, so Sessions share proxies once there are more of them, or once some cool down, unless `proxies.max_sessions` limits how many share one: with 1, no two do, and the Sessions that find none wait. `proxies.rebind = "every_lease"` binds a Session to another proxy at each Lease, so its Leases go through the proxies in turn. For sites that tie an Account to one address, `proxies.rebind = "retired"` has a Session wait for its proxy while it cools down, cooling down with it, and bind it to another only once it is Retired or no longer listed, and `"never"` Retires the Session with it, while the Run goes on with the others. `Request(proxy=...)` goes through its own instead, and `proxy=""` through none. A proxy that fails, with a `ProxyError`, cools down for `proxies.cooldown`, doubled with each failure in a row, and is Retired after `proxies.max_failures` in a row; its Request is retried through another, and its Sessions are bound to another. While every proxy cools down, Requests wait; once none ever can be used, the Run drains. Proxy credentials never appear in the logs or the stats.

How Sessions are bound is up to a binding policy, which `proxies.binding` replaces. Its `bind(binding)` is told of the Session, its Account, its proxy, why it is being bound, and the candidates, each with its health, tags, and Sessions, and returns the proxy to bind, or `None` to wait; the pool binds it only while it has room, and asks again if another Session took the last first. Returning `binding.current` keeps the Session on its proxy whatever becomes of it: while the proxy cools down, the Session waits for it, and once it is Retired or no longer listed, the Session is Retired with it.

```python
from pernix.proxies import Binding


class ByRegion:
    """Keeps each Account on its proxy while it is available, and binds it to one of its region."""

    async def bind(self, binding: Binding) -> str | None:
        if binding.reason == "lease":  # its proxy is available
            return binding.current
        region = REGIONS[binding.account.id] if binding.account else "us"
        return next((proxy.url for proxy in binding.open if region in proxy.tags), None)
```

A policy may also give a Session a proxy of its own, which the pool does not list, by returning a `Proxy` record: an Account's own, or a sticky session on a gateway built from the Session's id. The pool holds it for that Session alone: it takes no turns, no other Session goes through it, and refreshes keep it; its password is masked in the logs, and its health kept as any other's. Such a policy needs no source, but a Session Pool:

```python
from pernix.proxies import Binding, Proxy


class OwnAddress:
    """Sends each Account through its own proxy, from its `proxy` column of sessions.accounts_file."""

    async def bind(self, binding: Binding) -> str | Proxy | None:
        if binding.account is None:
            return None
        return Proxy(binding.account.extra["proxy"].reveal())  # or f"http://user-{binding.session}:pw@gate.example:7777"
```

With the default policy, a Session thus changes address, keeping its cookies, whenever its proxy cools down, is Retired, expires, or is no longer listed, or with `retired` once it is Retired, expired, or no longer listed: a refresh replaces the proxies, so with an API that answers with a new batch each time, every Session would change address at each refresh. Bindings last one Run.

APIs that hand out new short-lived proxies on request, extraction APIs, call for adding the proxies rather than replacing them, and extracting more as they run low, within limits, as extraction costs:

```python
class Shop(Spider):
    settings = {
        "proxies.api": "https://vendor.example/get?key=...&num={count}",  # {count}: how many are wanted
        "proxies.supply": "add",  # keep each proxy until it expires, so Sessions keep theirs
        "proxies.api_lifetime": 300,  # the answer, one per line, tells no expiry
        "proxies.min_available": 10,  # extract more while fewer are available, or when none fits
        "proxies.max_extracted": 20_000,  # at most this many in the Run
    }
```

With `proxies.supply = "add"`, each load adds the proxies the sources give, which stay until they expire, at the time their source gives, or after `proxies.api_lifetime` for those of the API. With `proxies.min_available` set too, the API and the provider are asked for more while fewer proxies are available, and when a fetch or a Session finds none: one extraction at a time, `proxies.extract_interval` seconds apart at least, each for as many as the level lacks, told through `{count}`, or to a provider whose `proxies()` takes a `wanted` keyword. Extraction stops at `proxies.max_extracted` proxies, and on demand after `proxies.extract_attempts` extractions in a row that gave none, as a vendor out of credit answers with an error; the Run drains for want of proxies only then. An API answering in JSON is read through `proxies.api_json`, which maps its fields: where the list of proxies is, as a path of keys joined by dots, a key of digits indexing a list; and, in each entry, the whole address, either the entry itself as a string or one of its fields, or else its host and port, with a user and a password if any; and when it expires, which overrides `proxies.api_lifetime`:

```toml
[proxies]
api = "https://vendor.example/get?key=...&num={count}&format=json"
supply = "add"

[proxies.api_json]      # an answer such as {"data": [{"ip": "...", "port": 8080, "deadline": "2027-01-01 12:00:00"}]}
list = "data"
host = "ip"
port = "port"
expires = "deadline"
expires_format = "datetime"   # or timestamp, timestamp_ms, or seconds, left
timezone = "Asia/Shanghai"    # of a date and time written without an offset; or a fixed offset, such as +08:00
```

An answer that does not match, such as an error, gives no proxy, with a warning naming the field it lacks, never the API's URL, and counts toward `proxies.extract_attempts`. Time zone names need the system's time zone data, which Windows lacks unless the `tzdata` package is installed; a fixed offset needs none. See [`docs/design.md`](docs/design.md) §5.3.

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

async with Search.stream_items(keyword="dune") as items:  # the Items that pass every Pipeline
    async for item in items:
        ...
```

Keyword arguments other than `settings=` and `output=` are Spider arguments, declared by the parameters of `__init__`. Strings, as from a command line, are converted to the annotated types: `"3"` to `3`, `"true"` to `True`, and JSON to lists, dicts, dataclasses, and Structs; `__init__` may raise `ValueError` to reject a value. `run()` and `crawl()` return a `RunResult` with the `run_id`, the `reason` the Run ended, and the final stats. `stream_items()` holds one Item at a time, so a slow consumer pauses the Callbacks; leaving the loop early stops the Run.

`crawler.stop()` drains a Run: no new Request is claimed, and the Requests in flight and the queued Items are finished within `shutdown.grace`. `crawler.stop("force")` cancels the Requests in flight and returns them to the Frontier, and discards the Items not processed yet; the Feeds are still completed. `run()` drains on the first SIGINT or SIGTERM and force-stops on the second, and ends the process with an exit code when the Run does not complete:

| Exit code | Meaning |
| --- | --- |
| `0` | The Run finished, or code stopped it |
| `1` | An error ended the Run; it is logged with its traceback |
| `2` | Invalid settings, Spider arguments, or components; nothing was fetched |
| `130` | A signal stopped the Run |

`crawl()` and `stream_items()` raise errors instead, and leave signals to your application. See [`docs/design.md`](docs/design.md) §4.12 and §6.6.

## Command line

```bash
pernix run books.py -a keyword=dune -s concurrency=4 -o books.jsonl   # Books.run() from the command line
pernix run myproject.spiders.books:Books                            # an import path, a Spider chosen by name
pernix fetch https://example.com/ > page.html                        # headers to stderr, the body to stdout
pernix fetch https://example.com/ --json                             # one JSON object, errors included
pernix fetch https://example.com/ --fetcher browser                  # the page as its scripts left it
pernix fetch https://example.com/ --extract markdown --max-chars 20000  # the main content, for a language model
pernix fetch https://example.com/ --css "article h2" --extract text  # only the parts a selector picks
pernix fetch -i urls.txt --json --links                             # JSON Lines, one object per URL
pernix shell https://example.com/                                    # `await fetch(...)`, `response.css(...)`
pernix check books.py                                                # validate settings and components
pernix browser install                                               # install the browser engine
```

`run` exits as `Spider.run()` does: 0 completed, 1 failed, 2 configuration error, 130 stopped by a signal. `-s KEY=VALUE` sets any setting, above every other layer, and `-a KEY=VALUE` passes a Spider argument. `fetch` and `shell` use the download Middleware and the Fetchers of the project settings; `--fetcher` picks a registered Fetcher, `browser` to render the page, and in `shell`, `await fetch(url, render=True)` renders one, the keywords going to `Request`. With the browser Fetcher registered, as it is by default, `check` launches the browser once, if the `browser` extra is installed, and reports an engine that is not installed with the command that installs it; it also tells whether the Profile and the browser are those of the Pinned Pair. `python -m pernix` works too.

`fetch` fetches its URLs as one small Run, so retries, the per-domain limits, and `concurrency`, with `browser.max_pages` for rendered pages, apply, but no Feeds, spider Middleware, or Pipelines of the project; `-s retry.times=0` makes one attempt. It reads pages cheaply for agents and scripts: `--extract text` gives the visible text, without scripts, styles, and hidden elements, and `--extract markdown` the main content as Markdown, its headings, lists, tables, and code blocks kept and its links made absolute, without the navigation, footers, and other boilerplate. `--css` and `--xpath`, repeated as needed, read the parts they pick instead, each as markup, or as text or Markdown with `--extract`; `--links` lists the links, as absolute URLs with their text. `--max-chars N` cuts what is read, and the JSON says `"truncated": true`. Several URLs, or `-i FILE` (`-` for stdin, one URL per line), are written as JSON Lines with `--json`, one object per URL, in the order they end, a failure as `{"url": ..., "error": {...}}`. `fetch` exits with 0 when every URL got a Response, whatever its status, 1 when any did not, and 2 when it cannot fetch at all, as without the browser engine. Pointed at untrusted links, `-s network.public_only=true` refuses hosts on private, loopback, link-local, and other addresses that are not public, each redirect hop included, checked once resolved, and the browser renders through a proxy that checks every host its pages reach. See [`docs/design.md`](docs/design.md) §4.12 for the `--json` fields.

## Configuration

Settings come from several layers, from lowest to highest precedence: built-in defaults, Addons, the project file, `PERNIX_*` environment variables, `Spider.settings`, the `settings=` argument of `run()`, `crawl()`, and `stream_items()`, and `-s` on the command line.

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
| `concurrency` | `16` | Maximum number of Requests processed at once that do not render; rendered ones have `browser.max_pages` workers of their own |
| `extensions` | `{OffsiteFilter: 100}` | Extensions to run, as `{extension: order}` |
| `addons` | `()` | Addons to enable, by entry point name or import path |
| `middlewares` | `{}` | Download Middleware, as `{middleware: order}`; lower orders are further out |
| `spider_middlewares` | `{}` | Spider Middleware, as `{middleware: order}`; lower orders are further out |
| `fetchers` | `{"http": HTTPFetcher}` | Fetchers by name; `Request(fetcher=name)` selects one |
| `pipelines` | `{}` | Item Pipelines, as `{pipeline: order}`; lower orders run first |
| `failed_pipelines` | `{FailedItemsFile: 100}` | Pipelines that receive the Items a Pipeline failed on, as `FailedItem`s |
| `feeds` | `{}` | Files Items are written to, as `{path_template: options}` |
| `run_id` | `None` | Identifies the Run in logs and stats; `None` generates one |
| `locale` | `["en-US", "en"]` | The languages the browser presents, as a list or `"en-US,en"`; the HTTP Fetcher sends the `Accept-Language` Chrome makes of them |
| `events.timeout` | `30.0` | Seconds before an async event subscriber is cancelled |
| `domain.concurrency` | `8` | Fetches in progress at once per domain (host) |
| `domain.delay` / `domain.delay_jitter` | `0.0` / `0.5` | Seconds between the starts of two fetches from one domain, randomized by ± the jitter |
| `domain.rate` / `domain.burst` | `None` / `1` | Fetches per second per domain (GCRA), and how many may start at once |
| `domain.cooldown_max` | `300.0` | The longest a 429 or 503 with `Retry-After` pauses its domain; with a Session Pool, a 429 cools its Session down instead |
| `sessions.enabled` | `false` | Whether Requests lease Sessions from a Session Pool |
| `sessions.pool` | `"default"` | The pool's name |
| `sessions.size` / `sessions.eager` | `8` / `false` | Sessions the pool holds at most without Accounts, created as Requests need them, or all at the start with `eager` |
| `sessions.max_concurrency` | `1` | Fetches in progress at once per Session, but for those bound to an Account, which have their own |
| `sessions.cooldown` / `sessions.cooldown_max` | `30.0` / `300.0` | Seconds a 429 without `Retry-After`, or a failed login, cools its Session down, and the longest any 429 does |
| `sessions.lease_ttl` | `300.0` | Seconds a Session stays leased without renewal; leases are renewed while their fetch runs |
| `sessions.accounts` / `sessions.accounts_file` | `()` / none | Accounts to bind Sessions to, and a JSON Lines or CSV file of more |
| `sessions.login_attempts` | `3` | Failed logins in a row after which a Session is Retired |
| `sessions.max_blocks` | `3` | Blocked Responses in a row after which a Session is Retired |
| `proxies.urls` | `()` | Proxies Requests go through, such as `http://user:password@proxy:8080`, or tables with a `url` and `tags` |
| `proxies.file` / `proxies.api` / `proxies.provider` | none | More proxies: a text file, an API answering with one per line (a `Secret`, `{count}` in it replaced by how many are wanted), and a `ProxyProvider` |
| `proxies.refresh` | `None` | Seconds between loads of the file, the API, and the provider; `None` loads them once |
| `proxies.supply` | `"replace"` | Whether each load replaces the proxies listed, or `add`s to them, keeping each until it expires |
| `proxies.api_lifetime` | `None` | Seconds each proxy of the API works for, unless its answer tells when it expires |
| `proxies.api_json` | `None` | How to read the API's answers in JSON: `list`, `address` or `host` and `port`, `username`, `password`, `scheme`, `expires`, `expires_format`, and `timezone` |
| `proxies.min_available` | `None` | With `add`, extract more from the API and the provider while fewer proxies are available, and when none fits |
| `proxies.extract_interval` / `proxies.max_extracted` / `proxies.extract_attempts` | `1.0` / `None` / `3` | Seconds between extractions on demand at least; proxies extracted in a Run at most; extractions in a row giving none, after which no more are made on demand |
| `proxies.cooldown` / `proxies.cooldown_max` | `60.0` / `600.0` | Seconds a failed proxy cools down, doubled with each failure in a row, and the longest it does |
| `proxies.max_failures` | `5` | Failures in a row after which a proxy is Retired |
| `proxies.max_sessions` | `None` | Sessions a proxy is bound to at most; with `1`, no two share one, and those that find none wait |
| `proxies.rebind` | `"unavailable"` | When the default binding policy binds a Session to another proxy: once its own is `unavailable`, at `every_lease`, once it is `retired` (waiting for it while it cools down), or `never` (Retiring the Session with it) |
| `proxies.binding` | `None` | A replacement binding policy, with `async bind(binding)`; it may give Sessions proxies of their own, with no source |
| `response.default_encoding` | `"utf-8"` | Encoding of a body that declares none, is not UTF-8, and defies detection |
| `network.public_only` | `false` | Refuses hosts whose addresses are not public, after resolving them, on every redirect hop; the browser goes through a proxy that checks every host it reaches, and renders nothing through a proxy of a Request or a Session |
| `network.allow_hosts` | `()` | Host names `network.public_only` lets through, such as an internal service |
| `frontier.lease_ttl` | `60.0` | Seconds a claimed Request stays leased without renewal |
| `fingerprint.headers` | `()` | Request headers whose values take part in Fingerprints; never `Cookie` |
| `fingerprint.sort_query` | `true` | Sort query parameters, so their order does not make a Request new |
| `fingerprint.function` | `None` | A replacement Fingerprint function, `(request) -> str` |
| `retry.times` | `3` | Retries after the first attempt; `0` turns retrying off |
| `retry.statuses` | `[408, 429, 500, 502, 503, 504, 522, 524]` | Response status codes that are retried |
| `retry.backoff_base` / `retry.backoff_max` | `1.0` / `60.0` | Full-jitter backoff: seconds of the first retry, doubling up to the maximum |
| `retry.retry_after_max` | `300.0` | The longest wait a `Retry-After` header can set |
| `retry.delay_scale` | `1.0` | Multiplies every retry delay; `0` retries at once, as in tests |
| `retry.browser_lost` | `3` | Retries after a browser went away (`BrowserLost`), at once, apart from `retry.times` |
| `retry.blocked` | `3` | Retries after a Response was found Blocked, at once, with another Session, through another proxy, or rendered, apart from `retry.times` |
| `blocked.statuses` / `blocked.render` | `[403]` / `false` | The statuses the default `Spider.is_blocked()` finds Blocked; whether an HTTP navigation found Blocked is retried rendered |
| `retry.policy` | `None` | A replacement retry policy, an object with `decide(request, outcome, attempt)` |
| `log.output` | `"auto"` | `"stderr"`, `"stdout"`, or `"root"` (the application's handlers); `"auto"` is `"root"` if logging is configured, else `"stderr"` |
| `log.format` | `"auto"` | `"text"`, `"json"` (JSON lines), or `"auto"`: text on a terminal, JSON lines otherwise |
| `log.level` | `"INFO"` | The lowest level Pernix writes |
| `log.queue_size` | `10000` | Records waiting to be written at most; more are dropped and counted |
| `log.redact` | `()` | Names whose values are masked in logs, besides the built-in ones |
| `stats.interval` | `60.0` | Seconds between stats snapshots; `0` keeps only the final one |
| `stats.sinks` | `{}` | Stats Sinks, as `{sink: order}`, that receive every snapshot |
| `loop.block_threshold` | `30.0` | Seconds the event loop may stay blocked before a warning logs the code blocking it; `0` turns the watchdog off |
| `callbacks.timeout` | `None` | Seconds a Callback, with the spider Middleware, or an errback may take to produce its outputs before it fails with `CallbackTimeout`; `None` for no limit |
| `items.queue_size` | `1000` | Items waiting for the Pipelines at most; a Callback producing more waits |
| `items.concurrency` | `1` | Items processed at once; with 1, Feeds keep the order Items were produced in |
| `items.timeout` | `60.0` | Seconds one Pipeline call may take before it fails with `TimeoutError`; `0` for no limit |
| `items.failed_output` | `None` | The JSON-lines file where `FailedItemsFile` writes failed Items, with the error |
| `http.impersonate` | `"chrome150"` | The browser Profile HTTP requests impersonate, a pinned version; `Request(impersonate=...)` overrides it. With the pinned one, the User-Agent, client hints, and `Accept-Language` are those of the bundled browser |
| `http.timeout` | `30.0` | Seconds to connect and receive the headers, those of each redirect apart, and between two pieces of the body |
| `http.deadline` | `180.0` | Seconds a whole fetch may take, waiting for a connection included |
| `http.max_redirects` | `20` | Redirects followed at most, as browsers follow them, before `TooManyRedirects` |
| `http.max_body_size` | `104857600` | Bytes a body may have at most (100 MiB); `0` for no limit |
| `http.pool_size` / `http.pool_idle` | `8` / `300.0` | curl sessions kept, one per Profile, and seconds one is kept unused |
| `http.inherit_referer` | `false` | Whether a Request constructed in a Callback without a `Referer` takes the page its Response belongs to |
| `browser.headless` | `true` | Runs the browser without a window, in Chrome's new headless mode |
| `browser.timeout` | `30.0` | Seconds a page may take to load; `Request(timeout=...)` overrides it |
| `browser.deadline` | `180.0` | Seconds a whole rendered fetch may take, starting the browser included, once it may open its page |
| `browser.launch_options` | `{}` | Options passed as they are to patchright's `launch()`, such as `executable_path` or `args` |
| `browser.max_pages` | `8` | Pages open at once, each that of one fetch, and the rendered Requests a Run processes at once |
| `browser.generation_age` / `browser.generation_pages` | `3600.0` / `1000` | Seconds, and pages opened, after which the browser is replaced by a new one; `0` turns each off |
| `browser.generation_memory` | `0` | MiB the browser's processes may take before it is replaced; `0` does not measure them |
| `browser.max_idle_contexts` / `browser.context_idle` | `8` / `300.0` | Browser contexts kept idle for Sessions between their fetches, at most, and seconds one may stay idle |
| `browser.block` | `[]` | Resource types no rendered page loads, such as `"image"` or `"font"`; `Render(block=...)` replaces them |
| `browser.action_timeout` | `10.0` | Seconds `Render(wait_for=...)` and each page action may take, unless the action sets its own `timeout` |
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

Callbacks, Middleware, Pipelines, and Extensions share one event loop, so code that never awaits, such as `time.sleep()`, a synchronous HTTP client, or a long parse, stops the whole Run while it runs: no fetch, Lease renewal, or stats report goes on meanwhile. Await instead; run blocking calls with `await asyncio.to_thread(...)`, which keeps the loop running, and long CPU-bound work in a process pool, with `loop.run_in_executor()`, which also runs it in parallel. Once the event loop has been blocked for `loop.block_threshold` (30 s), a warning logs the stack of the code that blocks it, and once the loop runs again, an INFO line tells how long it was blocked; `loop.blocks` and `loop.block_seconds` count the blocks and their seconds. See [`docs/design.md`](docs/design.md) §8.

## Testing

```python
import re

from pernix import Request, Spider
from pernix.errors import Timeout
from pernix.testing import FakeFetcher, crawl, make_response, run_callback


class Books(Spider):
    start_urls = ["https://shop.example/"]

    async def parse(self, response):
        for href in response.css("a::attr(href)").getall():
            yield Request(href, callback=self.parse_book)

    async def parse_book(self, response):
        yield {"title": response.css("h1::text").get()}


async def test_books():
    fake = FakeFetcher({
        "https://shop.example/": "<a href='/book/1'>Dune</a><a href='/book/2'>Emma</a>",
        re.compile(r"https://shop\.example/book/1"): "<h1>Dune</h1>",
        "https://shop.example/book/2": [Timeout("slow"), "<h1>Emma</h1>"],  # in turn, the last repeating
    })
    report = await crawl(Books, fetcher=fake)
    assert sorted(item["title"] for item in report.items) == ["Dune", "Emma"]
    assert report.stats["requests.retried"] == 1
    assert fake.unmatched == []  # Requests no route matched got 404


async def test_parse():
    outputs = await run_callback(Books(), make_response("https://shop.example/", "<a href='/book/1'>Dune</a>"))
    assert [request.url for request in outputs] == ["https://shop.example/book/1"]
```

`pernix.testing` runs Spiders without a network. `FakeFetcher` answers Requests from canned bodies, Responses, exceptions, or functions, by URL or pattern. `crawl()` runs a Spider to completion and reports its Items and stats, without the delays that would only make a test wait, and with `network.public_only` off, so that it reaches a `LoopbackServer`; `run_callback()` runs one Callback as a Run would. `crawl(fetcher=fake)` answers HTTP Requests only; `crawl(fetcher=fake, browser=fake)` answers rendered ones too, without starting a browser, and `make_response(url, body, browser=RenderResult(evaluated=[...]))` builds a rendered Response. `LoopbackServer`, also the `loopback_server` fixture, serves routes or a directory on 127.0.0.1 for tests that fetch for real.

Installing Pernix adds a pytest plugin that keeps the Fetchers off the network while each test and its fixtures run: only loopback hosts can be reached, redirects and the requests of rendered pages included, and a test during which another host was refused fails. curl and the browser do not use Python's sockets, so other network-blocking plugins cannot do this. Mark a test with `@pytest.mark.pernix_network` to let it reach the network, or set `pernix_network = allow` in the pytest configuration. See [`docs/design.md`](docs/design.md) §9.

## Documentation

- [`CONTEXT.md`](CONTEXT.md): terminology
- [`docs/design.md`](docs/design.md): features and interfaces
- [`docs/adr/`](docs/adr/): architectural decisions
- [`CHANGELOG.md`](CHANGELOG.md): notable changes

## Project layout

```
src/pernix/
├── __init__.py          public API: Spider, Request, Response, Middleware, Extension, ...
├── spider.py            Spider base class, Inline Fetch, and the run()/crawl()/stream_items() entry points
├── spider_arguments.py  checks and converts Spider arguments
├── spider_hooks.py      tells the Spider hooks a subclass overrides from those it inherits
├── crawler.py           Crawler: runs one Run, builds components with injection, stops it
├── run_assembly.py      which components a Run has, built and wired before opening; the download chain
├── engine.py            worker loop: claim, process, settle Leases; browser workers apart; idle detection, Drain
├── request_processor.py one attempt at a Request: fetch, retry decision, Callbacks
├── inline_fetch.py      Inline Fetch: fetching in place, retried in place, and streamed
├── sending.py           how a Request is sent: as it is, or through the Run's next proxy; Sender
├── shutdown.py          Drain, its grace period, and Force Stop
├── runner.py            run() as a program: exit codes
├── __main__.py          python -m pernix
├── stop_signals.py      SIGINT and SIGTERM stop a Run
├── run_result.py        RunResult, how a Run ended
├── callbacks.py         runs Callbacks and errbacks, collects their outputs
├── retry_policy.py      retry decisions, retry budgets, backoff, Retry-After
├── blocked_responses.py telling Blocked Responses, and the Request a retry renders instead
├── throttle.py          per-domain slots: concurrency, delay, rate, cooldowns
├── request.py           Request
├── request_kinds.py     the kind of request a Request is: a navigation, a form, a fetch, or a load
├── request_codec.py     persisting Requests: msgpack, Callbacks by name
├── response.py          Response, selectors, URL joining
├── headers.py           Headers
├── cookies.py           checks cookies, parses Set-Cookie headers, and applies them to cookies held by name
├── cookie_jar.py        Cookie, CookieJar: full cookie records, stored and matched as browsers do
├── session.py           Session, SessionState: cookies, storage, proxy, Profile, and Account
├── account.py           Account: login credentials; reading them from JSON Lines and CSV files
├── secret.py            Secret, a value redacted in logs and reprs
├── urls.py              URL clean-up, joining, normalization, and hosts as clients connect to them
├── text_decoding.py     decoding bodies: encoding declarations, labels, detection
├── fingerprint.py       Fingerprints of Requests
├── browser/
│   ├── render.py        Render: the options of a rendered Request
│   ├── page_method.py   PageMethod: a Spider method run on the page, optional or limited in time
│   ├── actions.py       WaitFor, Scroll, Click, Type, Evaluate, Screenshot
│   ├── resource_types.py the resource types a page may be kept from loading
│   ├── url_globs.py     URL globs, as the browser's route patterns read them
│   └── render_result.py RenderResult and CapturedResponse: what rendering gave
├── extraction/
│   ├── visible_text.py  the visible text of HTML, laid out in lines as browsers do
│   ├── markdown.py      HTML as Markdown, and a document's main content as Markdown
│   ├── main_content.py  where a document's main content is, and what in it is boilerplate
│   ├── links.py         the links of HTML, absolute, with their text
│   └── elements.py      the parts of lxml elements reading HTML uses
├── errors.py            exception hierarchy, control-flow exceptions
├── components.py        loads components from import paths and builds them
├── component_lifecycle.py opens components in order, closes them in reverse
├── addons.py            Addon loading, entry point discovery, setup API
├── cli/
│   ├── __init__.py      the pernix command: main()
│   ├── run_command.py   pernix run
│   ├── fetch_command.py pernix fetch: its options, and the Run of its URLs
│   ├── fetch_spider.py  the Spider of pernix fetch, reporting each URL as it ends
│   ├── fetch_output.py  JSON Lines or the Response for people, one URL at a time
│   ├── page_reading.py  what pernix fetch reads: text, Markdown, selected parts, links, cut to size
│   ├── shell_command.py pernix shell
│   ├── check_command.py pernix check, launching the browser once
│   ├── browser_command.py pernix browser install
│   ├── spider_loader.py finds a Spider in a file or an import path
│   ├── downloader.py    fetches single Requests outside a Run
│   ├── console.py       the interactive console with top-level await
│   └── options.py       -s and -a, and the Crawler of a command
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
│   ├── fetcher.py       Fetcher and StreamingFetcher protocols
│   ├── fetcher_dispatch.py picks the Fetcher of each Request, and tells rendered ones; opens streamed transfers
│   ├── http_fetcher.py  curl-cffi HTTP Fetcher, following redirects one hop at a time
│   ├── redirects.py     which responses redirect, where to, and how the request changes, as in browsers
│   ├── chain_cookies.py the cookies of a fetch, sent to each redirect hop in Chrome's order
│   ├── http_settings.py the http.* settings and the pinned Profile
│   ├── pinned_pair.py   the Pinned Pair: the bundled build's User-Agent, client hints, and Accept-Language
│   ├── chrome_headers.py the headers Chrome sends for each kind of request, and from which page
│   ├── browser_fetcher.py patchright browser Fetcher: loads pages, builds their Responses
│   ├── page_slots.py    the pages open at once, browser.max_pages, each taken by one fetch
│   ├── browser_generations.py the browsers one Generation after another: replaced, lost, probed, closed
│   ├── browser_processes.py the processes of a browser and its driver: memory, and killing them
│   ├── tripwire.py      cancels the work in its scopes once tripped, raising an error in its place
│   ├── session_contexts.py the browser contexts kept for Sessions, synced before and merged after each fetch
│   ├── browser_cookies.py cookies between a cookie jar and the browser, and what a page changed
│   ├── browser_settings.py the browser.* settings
│   ├── page_actions.py  runs a Render's page methods, wait, actions, and screenshot on its page
│   ├── kept_pages.py    the pages kept open for Callbacks, and the scope that closes them
│   ├── page_traffic.py  blocks resource types and captures responses of a page
│   ├── launched_browser.py starts patchright and its browser, under the network guard if on, as with a window, and probes it
│   ├── refusing_proxy.py the proxy that refuses what a guarded browser must not reach
│   ├── address_guard_proxy.py network.public_only for the browser: a SOCKS5 proxy that checks every host it reaches
│   ├── proxy_refusals.py what that proxy refused a page, told as errors, stats, and logs
│   ├── loopback_listener.py accepts the connections of those proxies on 127.0.0.1
│   ├── curl_session_pool.py one curl session per Profile, LRU and idle eviction; body limits and backpressure
│   ├── network_guard.py keeps Fetchers on loopback hosts, as in tests
│   ├── address_guard.py network.public_only: refuses hosts not on public addresses, once resolved
│   └── streamed_response.py a Response whose body is read as it arrives
├── frontiers/
│   ├── frontier.py      Frontier protocol, Lease
│   └── memory_frontier.py in-memory Frontier, rendered Requests queued apart
├── middlewares/
│   ├── middleware.py    download Middleware, NodeMiddleware, the download chain
│   └── spider_middleware.py spider Middleware and their chain
├── sessions/
│   ├── session_pool.py  SessionPool protocol, SessionLease, Outcome, StateDelta
│   ├── memory_session_pool.py MemorySessionPool: the in-process pool of a Run
│   ├── session_leasing.py leases a Session per fetch, binds its proxy, logs it in, reports how it fared, saves its changes
│   └── session_login.py logs Sessions in with the Spider's login, judges logged-out Responses
├── proxies/
│   ├── proxy_url.py     proxy URLs, in the one form that tells proxies apart
│   ├── proxy.py         Proxy, a proxy as its source gives it, with its tags
│   ├── proxy_pool.py    ProxyPool protocol, ProxyCandidate, ProxyHealth, ProxyCounts
│   ├── memory_proxy_pool.py MemoryProxyPool: turns, candidates, bindings, Sessions' own, cooldowns, and Retirements
│   ├── binding_policy.py BindingPolicy protocol, Binding, and the default policy
│   ├── proxy_sources.py ProxyProvider protocol; lists, files, and APIs of proxies
│   └── run_proxies.py   the proxies of a Run: loaded, refreshed, given to fetches, bound to Sessions, and reported on
├── stats/
│   ├── stats.py         Stats, the counters and values of a Run
│   ├── core_stats.py    counts Requests, downloads, Responses, and Items
│   ├── stats_reporter.py periodic and final snapshots, the summary
│   ├── loop_lag.py      samples the event loop lag
│   ├── loop_watchdog.py warns while the event loop is blocked, with the stack that blocks it
│   └── stats_sink.py    StatsSink protocol
├── settings/
│   ├── loader.py        merges and validates the settings layers
│   ├── sources.py       project file and environment variables
│   ├── settings.py      Settings, the immutable result
│   ├── section.py       Section base class, ComponentMap, NamedComponents
│   ├── core_settings.py the Sections every Crawler registers
│   └── conversion.py    validates values, converting strings from the environment
└── testing/
    ├── fake_fetcher.py  FakeFetcher: canned answers by URL or pattern
    ├── responses.py     make_response(), Responses for tests
    ├── crawling.py      crawl() and run_callback(): running Spiders and Callbacks in tests
    ├── loopback_server.py LoopbackServer: routes and files served on 127.0.0.1
    ├── pytest_plugin.py the pytest plugin: the network guard, the loopback_server fixture
    └── frontier.py      conformance suite for Frontier backends
tests/                   one test module per source module, end-to-end crawls, and the Profile contract
```

Pluggable component families with several implementations, such as Extensions, Feeds, Fetchers, Frontiers, Middleware, and Pipelines, each have a subpackage holding their protocol and the built-in implementations; the core model stays at the top level.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pernix browser install   # for the tests that render; they are skipped without it
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyright
```

curl-cffi and patchright are pinned to exact versions, as their Profile and browser build form a Pinned Pair: upgrade either only with `uv run pytest tests/test_profile_contract.py` passing, which needs the browser engine. See [ADR 0010](docs/adr/0010-consistency-over-synthetic-fingerprints.md).

## License

[Apache-2.0](LICENSE)
