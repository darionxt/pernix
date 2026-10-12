# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/) once released.

## [Unreleased]

### Added

- The full `Request` model: `json=` and `data=` bodies, `errback`, `kwargs` passed to the Callback, `priority`, `dont_filter`, `fingerprint`, `cookies`, per-Request `timeout` / `proxy` / `impersonate`, and component-private `ext`; plus `encode_request()` / `decode_request()` for persistent Frontiers, with Callbacks stored by method name and JSON-only user data. `Response.cookies` holds the cookies after a fetch, including those set on redirects, for `Request(url, cookies=response.cookies)`.
- Events: typed `Event` dataclasses dispatched as observe, gate, or transform; an `EventBus` with priorities, per-subscriber timeouts, isolation of failing subscribers, and `events.on()` for methods; core Run Events (`SpiderOpened`, `SpiderClosed`, `RequestScheduled`, `RequestDropped`, `RequestFailed`, `ResponseReceived`, `ItemScraped`, and more), emitted by the Engine and the Crawler.
- `Extension` components registered with the `extensions` setting, opened in order and closed in reverse, with close failures grouped in an `ExceptionGroup`.
- Addons enabled with the `addons` setting by entry point name or import path, with an `api_version` check, a settings layer, and a registration-only `setup()`.
- Layered settings: built-in defaults, Addons, `pernix.toml` / `[tool.pernix]`, `PERNIX_*` environment variables, `Spider.settings`, and run arguments, validated at startup with suggestions for misspelled keys and immutable afterwards. Typed `Section`s, `ComponentMap` registries, `Secret` values, and component construction with injection of the Crawler, the settings, and Sections.
- End-to-end HTTP crawl: `Spider`, immutable `Request`, `Response` with CSS/XPath selectors, `Headers`, an in-memory Frontier, a curl-cffi `HTTPFetcher`, a JSON-lines feed, and `Spider.run()` / `Spider.crawl()`.
- Project scaffolding: uv packaging, ruff, pyright, pytest, CI on Python 3.13 and 3.14, Apache-2.0 license.

### Changed

- Requests are independent: cookies no longer persist from one Request to another. Pass `cookies=` explicitly, or use a Session Pool once available (ADR 0011).
- `Spider.run()` and `Spider.crawl()` take `settings={"concurrency": ...}` instead of a `concurrency` argument.
