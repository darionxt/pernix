from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

from pernix.crawler import Crawler
from pernix.errors import CONFIGURATION_ERRORS
from pernix.exit_code import ExitCode
from pernix.stop_signals import StopSignals

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

    from pernix.run_result import RunResult
    from pernix.spider import Spider

__all__ = ["ExitCode", "run_program", "run_spider"]


def run_spider(
    spider_type: type[Spider],
    *,
    args: Mapping[str, object] | None = None,
    settings: Mapping[str, Any] | None = None,
    output: str | os.PathLike[str] | None = None,
) -> RunResult:
    """Run a Spider to completion in a new event loop, as the main program of a process.

    It is `run_program()`, ending the process when the Run does not complete.

    Args:
        spider_type: The Spider class.
        args: The Spider arguments, by name.
        settings: Settings for this Run.
        output: A Feed for the Items besides the `feeds` setting.

    Returns:
        How the Run ended, when it finished or code stopped it.

    Raises:
        SystemExit: The Run failed, was misconfigured, or was stopped by a signal.
    """
    code, result = run_program(spider_type, args=args, settings=settings, output=output)
    if result is None or code is not ExitCode.COMPLETED:
        raise SystemExit(code)
    return result


def run_program(
    spider_type: type[Spider],
    *,
    args: Mapping[str, object] | None = None,
    settings: Mapping[str, Any] | None = None,
    command_line: Mapping[str, str] | None = None,
    output: str | os.PathLike[str] | None = None,
) -> tuple[ExitCode, RunResult | None]:
    """Run a Spider to completion in a new event loop, and tell the exit status of the program.

    SIGINT and SIGTERM stop the Run, as `pernix.stop_signals.StopSignals` describes.
    A configuration error is written to stderr, or logged by the Run, without a
    traceback; a failure is logged with one.

    Args:
        spider_type: The Spider class.
        args: The Spider arguments, by name.
        settings: Settings for this Run.
        command_line: Settings given as text on the command line.
        output: A Feed for the Items besides the `feeds` setting.

    Returns:
        The exit status, and how the Run ended unless it failed or was misconfigured.
    """
    try:
        crawler = Crawler.from_spider(
            spider_type, args=args, settings=settings, command_line=command_line, output=output
        )
    except CONFIGURATION_ERRORS as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return ExitCode.CONFIGURATION_ERROR, None
    try:
        result, interrupted = asyncio.run(_crawl(crawler))
    except Exception as exc:  # the Run logged it
        return ExitCode.of(exc), None
    return ExitCode.of(None, interrupted=interrupted), result


async def _crawl(crawler: Crawler) -> tuple[RunResult, bool]:
    with StopSignals(crawler) as signals:
        result = await crawler.crawl()
    return result, signals.received > 0
