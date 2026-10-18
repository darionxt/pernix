from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pernix.cli.fetch_output import FetchOutput
from pernix.cli.fetch_spider import FetchPlan, FetchSpider, InvalidURL
from pernix.cli.options import UsageError, settings_parser
from pernix.cli.page_reading import ReadingOptions, check_selection
from pernix.crawler import Crawler
from pernix.request import Request
from pernix.runner import ExitCode
from pernix.stop_signals import StopSignals

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pernix.cli.page_reading import Selection

__all__ = ["add_parser"]

_RUN_SETTINGS: Mapping[str, Any] = MappingProxyType({"feeds": {}, "log.level": "CRITICAL", "stats.interval": 0})
"""Settings of the Run of `pernix fetch`: no Feeds, no periodic stats, and only critical log records, as it reports
each URL itself."""

_ITEM_COMPONENTS = ("spider_middlewares", "pipelines", "failed_pipelines")
"""The components of a project that handle Callbacks and Items, which `pernix fetch` has none of."""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix fetch`."""
    parser = commands.add_parser(
        "fetch",
        parents=[settings_parser()],
        help="fetch URLs and print their Responses, or what is read from them",
        description=(
            "Fetch URLs as one small Run, through the download Middleware and the Fetchers of the project settings, "
            "with retries, per-domain limits, and concurrency; --fetcher browser renders the pages. For one URL, the "
            "status line, redirects, and headers go to stderr, and the body, or what is read from it, to stdout. "
            "With --json, each URL is one JSON object on a line of stdout, its Response or its error; several URLs, "
            "or -i, need --json. Exits with 0 when every URL got a Response, whatever its status, 1 when any did "
            "not, and 2 for a configuration error, such as a browser engine that is not installed. Pointed at "
            "untrusted links, -s network.public_only=true refuses hosts that are not on public addresses."
        ),
    )
    parser.add_argument("url", nargs="*", help="absolute URLs to fetch")
    parser.add_argument(
        "-i", "--input", metavar="FILE", help="also fetch the URLs of FILE, one per line, or of stdin with -"
    )
    parser.add_argument(
        "--fetcher",
        metavar="NAME",
        help="the registered Fetcher to use, such as http, or browser to render the page; by default, the URL scheme's",
    )
    parser.add_argument("--timeout", type=_positive(float), metavar="SECONDS", help="seconds each fetch may take")
    parser.add_argument("--json", action="store_true", help="print each Response, or error, as one JSON object")
    parser.add_argument("-o", "--output", metavar="FILE", help="write the body, byte for byte, to FILE")
    parser.add_argument(
        "--extract",
        choices=("text", "markdown"),
        help="read the visible text, or the main content as Markdown; with selectors, of each part they pick",
    )
    parser.add_argument(
        "--css",
        dest="selectors",
        metavar="SELECTOR",
        type=lambda query: ("css", query),
        action="append",
        default=[],
        help="read the parts a CSS selector picks instead of the page; repeat it for more, kept in order",
    )
    parser.add_argument(
        "--xpath",
        dest="selectors",
        metavar="EXPR",
        type=lambda query: ("xpath", query),
        action="append",
        help="read the parts an XPath expression picks instead of the page; repeat it for more, kept in order",
    )
    parser.add_argument(
        "--links",
        action="store_true",
        help="read the links, as absolute URLs with their text, of the page or the parts selectors pick",
    )
    parser.add_argument(
        "--max-chars", type=_positive(int), metavar="N", help="read N characters at most, marking the cut as truncated"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Fetch the URLs and report each.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        UsageError: The options do not go together, a selector or a URL given as
            an argument is invalid, `-i` cannot be read, or the Fetcher is not
            registered.
        pernix.errors.ComponentError: A component cannot be built, or cannot fetch,
            as when the browser engine is not installed.
    """
    options = _reading_options(args)
    several = len(args.url) > 1 or args.input is not None
    if not args.url and args.input is None:
        raise UsageError("give a URL to fetch, or -i FILE")
    if several and not args.json:
        raise UsageError("several URLs, or -i, are reported as JSON Lines; add --json")
    if several and args.output is not None:
        raise UsageError("-o writes the body of one URL; give one URL without -i")
    entries: list[Request | InvalidURL] = [_request(url, args) for url in args.url]
    for url in _input_urls(args.input):
        try:
            entries.append(_request(url, args))
        except UsageError as exc:
            entries.append(InvalidURL(url, ValueError(str(exc))))
    output = FetchOutput(options, json=args.json, save_to=Path(args.output) if args.output is not None else None)
    crawler = _crawler(FetchPlan(tuple(entries), output), args.settings)
    fetchers = crawler.settings.named_components("fetchers")
    if args.fetcher is not None and args.fetcher not in fetchers:
        registered = ", ".join(repr(name) for name in sorted(fetchers)) or "none"
        raise UsageError(f"no Fetcher named {args.fetcher!r} is registered; registered: {registered}")
    interrupted = asyncio.run(_crawl(crawler))
    spider = crawler.spider
    if isinstance(spider, FetchSpider) and spider.configuration_error is not None:
        raise spider.configuration_error  # no fetch can succeed until it is fixed
    if interrupted:
        return ExitCode.INTERRUPTED
    return ExitCode.FAILED if output.failures or output.closed else ExitCode.COMPLETED


def _reading_options(args: argparse.Namespace) -> ReadingOptions:
    selectors: list[Selection] = args.selectors
    for selection in selectors:
        try:
            check_selection(selection)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
    return ReadingOptions(extract=args.extract, selectors=tuple(selectors), links=args.links, max_chars=args.max_chars)


def _request(url: str, args: argparse.Namespace) -> Request:
    try:
        return Request(url, fetcher=args.fetcher, timeout=args.timeout, dont_filter=True)
    except (TypeError, ValueError) as exc:
        raise UsageError(str(exc)) from exc


def _input_urls(path: str | None) -> list[str]:
    """The URLs of `-i`: one per line, but blank lines and those starting with `#`."""
    if path is None:
        return []
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"cannot read the URLs of {path}: {exc}") from exc
    return [line for line in map(str.strip, text.splitlines()) if line and not line.startswith("#")]


def _crawler(plan: FetchPlan, command_line: Sequence[tuple[str, str]]) -> Crawler:
    """The Crawler of the Run, with the project's settings, but without its Feeds, spider Middleware, and Pipelines.

    Components are disabled by name, so the settings are loaded a second time with
    those the project enables turned off.
    """
    given = dict(command_line)
    crawler = Crawler.from_spider(FetchSpider, args={"plan": plan}, settings=_RUN_SETTINGS, command_line=given)
    unused = {key: dict.fromkeys(crawler.settings.components(key)) for key in _ITEM_COMPONENTS}
    if not any(unused.values()):
        return crawler
    settings = {**_RUN_SETTINGS, **unused}
    return Crawler.from_spider(FetchSpider, args={"plan": plan}, settings=settings, command_line=given)


async def _crawl(crawler: Crawler) -> bool:
    """Run the Crawler, stopped by SIGINT and SIGTERM; whether a signal stopped it."""
    with StopSignals(crawler) as signals:
        await crawler.crawl()
    return signals.received > 0


def _positive(kind: Callable[[str], float]) -> Callable[[str], float]:
    """An argument type for a positive number, an `int` or a `float`."""

    def positive(text: str) -> float:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}") from None
        if value <= 0:
            raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}")
        return value

    return positive
