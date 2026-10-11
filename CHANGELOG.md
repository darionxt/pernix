# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/) once released.

## [Unreleased]

### Added

- Layered settings: built-in defaults, Addons, `pernix.toml` / `[tool.pernix]`, `PERNIX_*` environment variables, `Spider.settings`, and run arguments, validated at startup with suggestions for misspelled keys and immutable afterwards. Typed `Section`s, `ComponentMap` registries, `Secret` values, and component construction with injection of the Crawler, the settings, and Sections.
- End-to-end HTTP crawl: `Spider`, immutable `Request`, `Response` with CSS/XPath selectors, `Headers`, an in-memory Frontier, a curl-cffi `HTTPFetcher`, a JSON-lines feed, and `Spider.run()` / `Spider.crawl()`.
- Project scaffolding: uv packaging, ruff, pyright, pytest, CI on Python 3.13 and 3.14, Apache-2.0 license.

### Changed

- `Spider.run()` and `Spider.crawl()` take `settings={"concurrency": ...}` instead of a `concurrency` argument.
