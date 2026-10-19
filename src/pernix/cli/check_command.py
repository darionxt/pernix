from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.cli.options import command_crawler, settings_parser, spider_arguments_parser
from pernix.cli.spider_loader import load_spider
from pernix.errors import ComponentError
from pernix.fetchers import DEFAULT_PROFILE, BrowserFetcher, BrowserSettings, HTTPFetcher, HTTPSettings
from pernix.fetchers.launched_browser import probe_browser, require_patchright
from pernix.logs import RunContext, RunLog
from pernix.run_assembly import RunAssembly
from pernix.runner import ExitCode
from pernix.settings import CoreSettings, LogSettings
from pernix.settings.sources import find_project_file

if TYPE_CHECKING:
    import argparse

    from pernix.crawler import Crawler

__all__ = ["add_parser"]

_ROLES = (
    ("Stats Sinks", "stats.sinks"),
    ("Extensions", "extensions"),
    ("Download Middleware", "middlewares"),
    ("Spider Middleware", "spider_middlewares"),
    ("Pipelines", "pipelines"),
    ("Failure Pipelines", "failed_pipelines"),
)


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix check`."""
    parser = commands.add_parser(
        "check",
        parents=[settings_parser(), spider_arguments_parser()],
        help="validate the settings and the components",
        description=(
            "Load the settings and build every component without opening any, reporting every problem found. "
            "With TARGET, also find the Spider and convert its arguments. With the browser Fetcher, launch the "
            "browser once, if pernix[browser] is installed. Tell whether the Profile and the browser are those of "
            "the Pinned Pair. Exits with 0 when everything is valid, and 2 otherwise."
        ),
    )
    parser.add_argument(
        "target", nargs="?", metavar="TARGET[:Name]", help="a Spider, as for pernix run; by default, none"
    )
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Validate the settings and the components, and print a summary.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        pernix.cli.options.UsageError: The Spider cannot be found.
        pernix.errors.SettingsError: The settings are invalid.
        pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
        pernix.errors.ComponentError: A component cannot be built, or the browser
            engine is not installed or does not start.
    """
    spider_type = load_spider(args.target) if args.target is not None else None
    crawler = command_crawler(args.settings, spider_type, args.spider_args)
    run_log = RunLog(crawler.settings.section(LogSettings), RunContext(crawler.run_id, crawler.spider.name))
    assembly = RunAssembly.build(crawler, run_log)
    profile = browser = None
    if any(isinstance(component, HTTPFetcher) for component in assembly.components):
        profile = _profile(crawler)
    if any(isinstance(component, BrowserFetcher) for component in assembly.components):
        browser = _browser(crawler)
    project = find_project_file(Path.cwd())
    lines = ["OK", f"Project file: {project.origin if project is not None else 'none'}"]
    if spider_type is not None:
        lines.append(f"Spider: {spider_type.__qualname__} (name {spider_type.name!r})")
    lines.extend(_components(crawler, profile, browser))
    lines.append(f"Feeds: {', '.join(str(feed.path) for feed in assembly.feeds) or 'none'}")
    sys.stdout.write("\n".join(lines) + "\n")
    return ExitCode.COMPLETED


def _profile(crawler: Crawler) -> str:
    """Describe the Profile the HTTP Fetcher impersonates, and whether it is that of the Pinned Pair."""
    profile = crawler.settings.section(HTTPSettings).impersonate
    return f"{profile} (pinned)" if profile == DEFAULT_PROFILE else f"{profile} (not the pinned {DEFAULT_PROFILE})"


def _browser(crawler: Crawler) -> str:
    """Describe the browser the browser Fetcher launches, launching it once, if patchright is installed.

    A project that never renders has no use for the `browser` extra, so its absence
    is noted, not reported as a problem.

    Raises:
        pernix.errors.ComponentError: patchright is installed, but the browser engine
            is not, or the browser does not start.
    """
    try:
        require_patchright()
    except ComponentError as exc:
        return f"not installed ({exc})"
    locale = crawler.settings.section(CoreSettings).locale
    return asyncio.run(probe_browser(crawler.settings.section(BrowserSettings), locale=locale))


def _components(crawler: Crawler, profile: str | None, browser: str | None) -> list[str]:
    settings = crawler.settings
    fetchers = [f"{name} ({_label(spec)})" for name, spec in settings.named_components("fetchers").items()]
    lines = [f"Fetchers: {', '.join(fetchers) or 'none'}"]
    if profile is not None:
        lines.append(f"Profile: {profile}")
    if browser is not None:
        lines.append(f"Browser: {browser}")
    for title, key in _ROLES:
        lines.append(f"{title}: {', '.join(_label(spec) for spec in settings.components(key)) or 'none'}")
    return lines


def _label(spec: object) -> str:
    if isinstance(spec, str):
        return spec
    return getattr(spec, "__qualname__", None) or type(spec).__qualname__  # classes and functions, or objects
