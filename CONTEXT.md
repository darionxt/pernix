# Pernix

An asyncio-based Python crawling framework. HTTP and the browser are peer ways of fetching, the kernel is minimal, and everything else is a replaceable component. Project management and job scheduling are out of scope.

## Language

### Crawling

**Spider**:
The user-written crawling logic: where to start, how to parse a Response, and what to produce.
_Avoid_: crawler script, Crawler

**Request**:
An immutable description of one fetch, including which Callback receives the result.
_Avoid_: task, job, BrowserRequest

**Response**:
The unified result of one fetch, whether it came from HTTP or the browser.

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

**Fingerprint**:
The key that decides whether two Requests are duplicates.
_Avoid_: dedup key, hash, and "fingerprint" for client characteristics (that is a Profile)

### Identity

**Session**:
The identity a Request is sent under: cookies and browser storage state, a bound proxy, a Profile, and optionally an Account. HTTP and the browser share the same Session.
_Avoid_: identity, client session

**Session Pool**:
A named set of Sessions that multiple Workers and Spiders can share.
_Avoid_: account pool, cookie pool (they are special cases of a Session Pool)

**Profile**:
A mutually consistent set of client characteristics: TLS, HTTP/2, request headers, and UA version.
_Avoid_: browser fingerprint, fingerprint

**Account**:
Login credentials for a target site, which can be bound to a Session.
_Avoid_: user

**Lease**:
A time-limited claim on a Session or a Request. It carries a token and becomes invalid once it expires.
_Avoid_: lock, checkout

**Cooldown**:
A period during which a Session is paused because it was Rate Limited. It ends automatically.

**Retired**:
The state of a Session that has been permanently taken out of use because it was Blocked or otherwise invalid.
_Avoid_: banned, dead

**Blocked**:
A response in which the target site refuses to serve the current Session. It causes a switch to another Session.
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

**Drain**:
Stop claiming new Requests, finish the in-flight ones, then end.
_Avoid_: graceful stop

**Force Stop**:
Cancel in-flight Requests, return them to the Frontier, then end.

**Pause**:
Stop claiming new Requests while the Worker stays alive.

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
The ordered chain an Item passes through for validation, cleaning, and storage.

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

### Observability

**Stats**:
Named counters and values describing a Run, such as Responses per status code. Each Worker keeps them in memory and reports them as snapshots.
_Avoid_: metrics

**Stats Sink**:
A component that receives every stats snapshot of a Run to report it elsewhere, such as to the Platform.
_Avoid_: stats exporter
