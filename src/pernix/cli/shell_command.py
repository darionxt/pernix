from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING, Any

from pernix.cli.console import AsyncConsole, LoopThread
from pernix.cli.downloader import Downloader
from pernix.cli.options import command_crawler, settings_parser
from pernix.request import Request
from pernix.runner import ExitCode

if TYPE_CHECKING:
    import argparse

    from pernix.response import Response

__all__ = ["add_parser"]

BANNER = """\
pernix shell: code may use await at the top level.
  request, response              the last Request and its Response
  settings                       the settings
  await fetch(url_or_request)    fetches, and updates request and response"""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix shell`."""
    parser = commands.add_parser(
        "shell",
        parents=[settings_parser()],
        help="explore Responses in an interactive console",
        description=(
            "Start an interactive console with top-level await, preloaded with request, response, settings, and "
            "fetch(). Fetches go through the download Middleware and the Fetchers of the project settings."
        ),
    )
    parser.add_argument("url", nargs="?", help="a URL to fetch first; by default, none")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Start the console, fetching the URL first if one is given.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.
    """
    crawler = command_crawler(args.settings)
    downloader = Downloader(crawler)
    loop = LoopThread()
    try:
        loop.run(downloader.__aenter__())
        try:
            _interact(loop, downloader, {"request": None, "response": None, "settings": crawler.settings}, args.url)
        finally:
            loop.run(downloader.__aexit__(None, None, None))
    finally:
        loop.close()
    return ExitCode.COMPLETED


def _interact(loop: LoopThread, downloader: Downloader, namespace: dict[str, Any], url: str | None) -> None:
    async def fetch(target: str | Request) -> Response:
        request = target if isinstance(target, Request) else Request(target)
        response = await downloader.fetch(request)
        namespace["request"], namespace["response"] = request, response
        return response

    namespace["fetch"] = fetch
    if url is not None:
        try:
            loop.run(fetch(url))
        except Exception as exc:
            sys.stderr.write(f"Error: fetching {url} failed: {type(exc).__name__}: {exc}\n")
    banner = BANNER if namespace["response"] is None else f"{BANNER}\nresponse: {namespace['response']!r}"
    with contextlib.suppress(SystemExit):  # exit() and quit() leave the console
        AsyncConsole(namespace, loop.run).interact(banner=banner, exitmsg="")
