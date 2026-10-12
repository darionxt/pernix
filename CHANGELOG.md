# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/) once released.

## [Unreleased]

### Added

- Onion Middleware (ADR 0002): download `Middleware` (classes, or functions with `@middleware`) registered with `middlewares`, and spider `SpiderMiddleware` (async generators, or `@spider_middleware`) registered with `spider_middlewares`, built and checked once at startup and opened with the Run like Extensions. `NodeMiddleware` adapts `process_request` / `process_response` / `process_exception` hooks.
- Fetchers registered by name with the `fetchers` setting and selected per Request: `Request(fetcher=...)`, then `browser` for rendering, then the URL scheme a Fetcher declares in `schemes`. Settings gain `NamedComponents` registries, merged name by name.
- Control-flow exceptions `IgnoreRequest`, `Retry`, and `DropItem`; `IgnoreRequest` from download Middleware or a Fetcher emits the new `RequestIgnored` Event.
- The full `Response`: immutable with `replace()`; text decoding that follows browsers (explicit encoding, BOM, HTTP charset, `<meta>` or XML declaration, UTF-8, detection, then the `response.default_encoding` setting) with WHATWG label mapping, recorded in `encoding` and `encoding_source`; selectors chosen by media type with `re()`, `re_first()`, and `jmespath()`; cached `json()`; `urljoin()` honoring `<base href>`; and the `history`, `elapsed`, `ip_address`, `http_version`, `fetcher`, and `session` fields, filled in by the HTTP Fetcher where known.
- Relative URLs in Requests constructed by a Callback resolve against its Response, after redirects and honoring `<base href>`; elsewhere, such as in `start()`, or against a Response whose URL cannot serve as a base, a relative URL is an error. Whitespace, tabs, and newlines are removed from every URL.
- A Section can check its values in `__post_init__`; the problems are reported with the other settings problems.
- The full `Request` model: `json=` and `data=` bodies, `errback`, `kwargs` passed to the Callback, `priority`, `dont_filter`, `fingerprint`, `cookies`, per-Request `timeout` / `proxy` / `impersonate`, and component-private `ext`; plus `encode_request()` / `decode_request()` for persistent Frontiers, with Callbacks stored by method name and JSON-only user data. `Response.cookies` holds the cookies after a fetch, including those set on redirects, for `Request(url, cookies=response.cookies)`.
- Events: typed `Event` dataclasses dispatched as observe, gate, or transform; an `EventBus` with priorities, per-subscriber timeouts, isolation of failing subscribers, and `events.on()` for methods; core Run Events (`SpiderOpened`, `SpiderClosed`, `RequestScheduled`, `RequestDropped`, `RequestFailed`, `ResponseReceived`, `ItemScraped`, and more), emitted by the Engine and the Crawler.
- `Extension` components registered with the `extensions` setting, opened in order and closed in reverse, with close failures grouped in an `ExceptionGroup`.
- Addons enabled with the `addons` setting by entry point name or import path, with an `api_version` check, a settings layer, and a registration-only `setup()`.
- Layered settings: built-in defaults, Addons, `pernix.toml` / `[tool.pernix]`, `PERNIX_*` environment variables, `Spider.settings`, and run arguments, validated at startup with suggestions for misspelled keys and immutable afterwards. Typed `Section`s, `ComponentMap` registries, `Secret` values, and component construction with injection of the Crawler, the settings, and Sections.
- End-to-end HTTP crawl: `Spider`, immutable `Request`, `Response` with CSS/XPath selectors, `Headers`, an in-memory Frontier, a curl-cffi `HTTPFetcher`, a JSON-lines feed, and `Spider.run()` / `Spider.crawl()`.
- Project scaffolding: uv packaging, ruff, pyright, pytest, CI on Python 3.13 and 3.14, Apache-2.0 license.

### Changed

- The `Fetcher` protocol is `async def fetch(request)` plus `schemes`; Fetchers are opened and closed like other components and no longer check `render` or `fetcher` themselves.
- Requests are independent: cookies no longer persist from one Request to another. Pass `cookies=` explicitly, or use a Session Pool once available (ADR 0011).
- `Spider.run()` and `Spider.crawl()` take `settings={"concurrency": ...}` instead of a `concurrency` argument.
