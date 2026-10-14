from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from pernix.cli.downloader import Downloader
from pernix.cli.options import UsageError, command_crawler, settings_parser
from pernix.request import Request
from pernix.runner import ExitCode

if TYPE_CHECKING:
    import argparse

    from pernix.response import Response

__all__ = ["add_parser", "describe_response", "is_text"]

_TEXT_SUBTYPES = frozenset({"json", "xml", "javascript", "ecmascript", "x-www-form-urlencoded", "x-javascript"})


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix fetch`."""
    parser = commands.add_parser(
        "fetch",
        parents=[settings_parser()],
        help="fetch one URL and print the Response",
        description=(
            "Fetch one URL through the download Middleware and the Fetchers of the project settings, without "
            "retries. The status line, redirects, and headers go to stderr, and the body to stdout; with --json, "
            "one JSON object goes to stdout. Exits with 0 when a Response arrived, whatever its status, 1 when "
            "fetching failed, and 2 for a configuration error."
        ),
    )
    parser.add_argument("url", help="the absolute URL to fetch")
    parser.add_argument(
        "--fetcher", metavar="NAME", help="the registered Fetcher to use, such as http; by default, the URL scheme's"
    )
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="seconds the fetch may take")
    parser.add_argument("--json", action="store_true", help="print the Response, or the error, as one JSON object")
    parser.add_argument("-o", "--output", metavar="FILE", help="write the body, byte for byte, to FILE")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Fetch the URL and print the Response.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        UsageError: The URL or the Fetcher is invalid.
    """
    crawler = command_crawler(args.settings)
    fetchers = crawler.settings.named_components("fetchers")
    if args.fetcher is not None and args.fetcher not in fetchers:
        registered = ", ".join(repr(name) for name in sorted(fetchers)) or "none"
        raise UsageError(f"no Fetcher named {args.fetcher!r} is registered; registered: {registered}")
    try:
        request = Request(args.url, fetcher=args.fetcher, timeout=args.timeout)
    except (TypeError, ValueError) as exc:
        raise UsageError(str(exc)) from exc
    downloader = Downloader(crawler)
    try:
        response = asyncio.run(_fetch(downloader, request))
    except Exception as exc:
        _report_error(args, exc)
        return ExitCode.FAILED
    output = Path(args.output) if args.output is not None else None
    if output is not None:
        output.write_bytes(response.body)
    if args.json:
        _write_json(describe_response(response, include_body=output is None))
    else:
        _print_response(response, output)
    return ExitCode.COMPLETED


async def _fetch(downloader: Downloader, request: Request) -> Response:
    async with downloader:
        return await downloader.fetch(request)


def describe_response(response: Response, *, include_body: bool = True) -> dict[str, Any]:
    """Describe a Response as the JSON object of `pernix fetch --json`.

    Args:
        response: The Response.
        include_body: Whether to include a text body.

    Returns:
        `url` (requested), `final_url`, `status`, `http_version`, `headers` as
        `[name, value]` pairs, `encoding`, `elapsed` in seconds, `redirects` as
        `{url, status}` objects, `fetcher`, `body` (the decoded text, or `None` for a
        binary body or one left out), and `body_bytes`.
    """
    text = is_text(response)
    return {
        "url": response.request.url,
        "final_url": response.url,
        "status": response.status,
        "http_version": response.http_version,
        "headers": [[name, value] for name, value in response.headers.items()],
        "encoding": response.encoding if text else None,
        "elapsed": response.elapsed,
        "redirects": [{"url": redirect.url, "status": redirect.status} for redirect in response.history],
        "fetcher": response.fetcher,
        "body": response.text if text and include_body else None,
        "body_bytes": len(response.body),
    }


def is_text(response: Response) -> bool:
    """Tell whether a body is text: by its media type, or, without one, by holding no NUL byte."""
    content_type = response.headers.get_all("Content-Type")
    if not content_type:
        return b"\x00" not in response.body[:4096]
    media_type = content_type[-1].partition(";")[0].strip().lower()
    kind, _, subtype = media_type.partition("/")
    return kind == "text" or subtype in _TEXT_SUBTYPES or subtype.endswith(("+json", "+xml"))


def _print_response(response: Response, output: Path | None) -> None:
    err = sys.stderr
    for redirect in response.history:
        err.write(f"{redirect.status} {redirect.url}\n")
    elapsed = f"{response.elapsed:.2f} s, " if response.elapsed is not None else ""
    err.write(
        f"{response.http_version or 'HTTP'} {response.status} {response.url} ({elapsed}{len(response.body)} bytes)\n"
    )
    for name, value in response.headers.items():
        err.write(f"{name}: {value}\n")
    err.write("\n")
    if output is not None:
        err.write(f"Saved the body to {output}\n")
    elif is_text(response):
        sys.stdout.write(response.text)
    elif sys.stdout.isatty():
        err.write("The body is binary; save it with -o FILE\n")
    else:
        sys.stdout.flush()
        sys.stdout.buffer.write(response.body)


def _report_error(args: argparse.Namespace, exc: Exception) -> None:
    if args.json:
        error = {"type": type(exc).__name__, "message": str(exc), "code": getattr(exc, "code", None)}
        _write_json({"url": args.url, "error": error})
    else:
        sys.stderr.write(f"Error: fetching {args.url} failed: {type(exc).__name__}: {exc}\n")


def _write_json(value: dict[str, Any]) -> None:
    sys.stdout.write(orjson.dumps(value).decode() + "\n")
