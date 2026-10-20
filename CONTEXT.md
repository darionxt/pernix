# Pernix

An asyncio-based Python crawling framework. HTTP and the browser are peer ways of fetching, the kernel is minimal, and everything else is a replaceable component. Project management and job scheduling are out of scope.

## Language

### Crawling

**Spider**:
The user-written crawling logic: where to start, how to parse a Response, and what to produce.
_Avoid_: crawler script, Crawler

**Spider Argument**:
A value given to one Run of a Spider, such as a search keyword, declared by a parameter of the Spider's `__init__`. Strings from a command line or the Platform are converted to the declared type.
_Avoid_: spider parameter, run argument (settings for a Run are not Spider Arguments)

**Request**:
An immutable description of one fetch, including which Callback receives the result.
_Avoid_: task, job, BrowserRequest

**Response**:
The unified result of one fetch, whether it came from HTTP or the browser.

**Request Kind**:
The kind of request a browser would make a Request as: a top-level navigation, a form submission, an iframe's navigation, a fetch, or the load of a style sheet, a script, or an image. A Request's method and body tell it, or its `Sec-Fetch-Dest` header names it, and it decides the headers the HTTP Fetcher sends with the pinned Profile.
_Avoid_: request type, resource type (what a rendered page loads, which Render can block)

**Callback**:
A Spider method that handles a Response and produces Requests and Items.
_Avoid_: parser, handler

**Inline Fetch**:
A fetch awaited directly inside a Callback. It bypasses the Frontier: no deduplication, no persistence.
_Avoid_: synchronous request, sub-request

**Item**:
One piece of data produced by a Callback; any data object qualifies.
_Avoid_: record, result

**Render**:
Loading a page in the browser and running page interactions. It is an attribute of a Request, not a separate kind of request.
_Avoid_: dynamic request, browser request

**Fetcher**:
A transport that turns a Request into a Response, such as `http` or `browser`.
_Avoid_: downloader, client

**Generation**:
One browser and the patchright driver running it, from its launch until it is closed. The current one takes the new rendered fetches; it is replaced as it ages, or lost when it disconnects, its driver dies, or it stops answering, and the fetches in a lost one end with `BrowserLost`.
_Avoid_: browser instance, browser restart (a restart is a new Generation)

**Fingerprint**:
The key that decides whether two Requests are duplicates.
_Avoid_: dedup key, hash, and "fingerprint" for client characteristics (that is a Profile)

### Identity

**Session**:
The identity a Request is sent under: cookies and browser storage state, the client hints each origin asked for, a bound proxy, a Profile, and optionally an Account. HTTP and the browser share the same Session.
_Avoid_: identity, client session

**Session Pool**:
A named set of Sessions that multiple Workers and Spiders can share.
_Avoid_: account pool, cookie pool (they are special cases of a Session Pool)

**Proxy Pool**:
The proxies a Run sends Requests through, from its sources, with the health of each and the Sessions bound to each. Its sources may replace the proxies at each load, or add to them, each kept until it expires; proxies may be extracted on demand from an API as they run low, within limits, as extraction costs. A Session is bound to one proxy, as the Run's binding policy picks it, by default keeping it while that is available; the policy may keep it on its proxy whatever happens, so the Session follows it, or give it a proxy of its own, which no other Session goes through. A Request without a Session goes through the next one in turn.
_Avoid_: proxy list, rotator

**Profile**:
A mutually consistent set of client characteristics: TLS, HTTP/2, request headers, and UA version.
_Avoid_: browser fingerprint, fingerprint

**Pinned Pair**:
The browser build the browser Fetcher launches and the Profile the HTTP Fetcher impersonates by default, pinned together, which the Profile contract tests show to present one client on the wire.
_Avoid_: fingerprint pair, matching versions

**Account**:
Login credentials for a target site, which can be bound to a Session.
_Avoid_: user

**Login**:
Bringing a Session into a site's logged-in state, with its Account if it has one, before the Session is first used and again once a Response shows it logged out. A Session logs in once at a time.
_Avoid_: sign-in, authentication

**Lease**:
A time-limited claim on a Session or a Request. It carries a token and becomes invalid once it expires.
_Avoid_: lock, checkout

**Cooldown**:
A period during which a Session or a proxy is paused: a Session because it was Rate Limited, its Login failed, or it waits for its proxy, which cools down; a proxy because it failed. It ends automatically.

**Retired**:
The state of a Session or a proxy that has been permanently taken out of use: a Session because it was Blocked or otherwise invalid, or with the proxy it follows; a proxy because it failed too many times in a row.
_Avoid_: banned, dead

**Blocked**:
A response in which the target site refuses to serve the current Session, or its proxy. It causes a switch to another Session, or another proxy, under a retry budget of its own; a Session Blocked too many times in a row is Retired.
_Avoid_: banned

**Rate Limited**:
A response in which the target site asks the client to slow down. It causes a Cooldown, never a Retirement.
_Avoid_: blocked

### Scheduling and runs

**Run**:
One execution of a Spider from start to finish, identified by a `run_id`. A Run can span multiple Workers and multiple restarts (resumed crawls).
_Avoid_: job, task

**Worker**:
A single process taking part in a Run.
_Avoid_: node, instance

**Frontier**:
The store of Requests waiting to be fetched in a Run. It handles ordering, deduplication, and Leases.
_Avoid_: queue, scheduler

**Dead Request**:
A Request that exhausted its retries or cannot be processed. It is kept in the Frontier for inspection, never deleted.
_Avoid_: failed queue

**Retry Budget**:
Retries a Request may have for one kind of failure that is not its own, such as a lost browser, counted apart from its attempts, so they take none of its ordinary retries.
_Avoid_: retry quota

**Drain**:
Stop claiming new Requests, finish the in-flight ones, then end.
_Avoid_: graceful stop

**Force Stop**:
Cancel in-flight Requests, return them to the Frontier, discard the Items not processed yet, then end.
_Avoid_: kill

**Grace Period**:
How long a Drain may take before it turns into a Force Stop.

**Pause**:
Stop claiming new Requests while the Worker stays alive, finishing the in-flight ones, until resumed. A paused Run never ends on its own; a Drain or a Force Stop still ends it.
_Avoid_: suspend

**Finite Run**:
A Run that ends automatically once the Frontier is drained.

**Continuous Run**:
A Run that never ends on its own and can only be stopped from outside.

### Extension

**Crawler**:
The runtime object that hosts a Run inside one Worker. It holds the settings, the event bus, and all components.
_Avoid_: Engine (the Engine is the scheduling loop inside a Crawler)

**Middleware**:
An onion-style layer wrapped around fetching (download Middleware) or around a Callback (spider Middleware).
_Avoid_: hook, interceptor

**Pipeline**:
A component that checks, cleans, or stores Items; Items pass through the Pipelines in order.

**Failed Item**:
An Item a Pipeline failed on, handed with the error to the failure Pipelines, for example to be written to a file and processed again later.
_Avoid_: dead letter (except for a destination such as a queue)

**Feed**:
Built-in file output, such as jsonl or csv.
_Avoid_: exporter

**Event**:
A typed notification emitted during the lifecycle of a Run.
_Avoid_: signal, hook

**Extension**:
A component that subscribes to Events and has its own lifecycle.

**Addon**:
A distributable unit that bundles settings changes and component registrations.
_Avoid_: plugin

**Platform**:
The external system that manages projects and starts and stops Runs. Pernix only exposes observation and control interfaces to it.
_Avoid_: scheduler

**Control Source**:
A component that tells the state a Platform wants a Run in, running, paused, drained, or force-stopped, such as from a file the Platform writes. Pernix reads it and pauses, resumes, or stops the Run as it changes.
_Avoid_: command channel

### Observability

**Stats**:
Named counters and values describing a Run, such as Responses per status code. Each Worker keeps them in memory and reports them as snapshots.
_Avoid_: metrics

**Stats Sink**:
A component that receives every stats snapshot of a Run to report it elsewhere, such as to the Platform.
_Avoid_: stats exporter

**Outcome File**:
A JSON document a Run writes as its last step, however it ends, telling a Platform how it ended: its reason, its exit code, its stats, and the error it failed with.
_Avoid_: result file, status file
