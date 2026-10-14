from __future__ import annotations

from typing import TYPE_CHECKING

from pernix.cli.options import settings_parser, spider_arguments_parser
from pernix.cli.spider_loader import load_spider
from pernix.runner import run_program

if TYPE_CHECKING:
    import argparse

__all__ = ["add_parser"]


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix run`."""
    parser = commands.add_parser(
        "run",
        parents=[settings_parser(), spider_arguments_parser()],
        help="run a Spider",
        description=(
            "Run a Spider as Spider.run() does. The first SIGINT or SIGTERM drains the Run, and the second stops "
            "it now. Exits with 0 when the Run completed, 1 when it failed, 2 for a configuration error, and 130 "
            "when a signal stopped it."
        ),
    )
    parser.add_argument(
        "target",
        metavar="TARGET[:Name]",
        help=(
            "a .py file or an import path, optionally followed by :Name, a Spider class name or Spider.name; "
            "Name may be left out when TARGET defines one Spider"
        ),
    )
    parser.add_argument("-o", "--output", metavar="PATH", help="a Feed for the Items: a .jsonl, .csv, or .json file")
    parser.add_argument("--run-id", metavar="ID", help="identifies the Run, as the run_id setting")
    parser.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    """Run the Spider.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        pernix.cli.options.UsageError: The Spider cannot be found.
    """
    spider_type = load_spider(args.target)
    command_line = dict(args.settings)
    if args.run_id is not None:
        command_line["run_id"] = args.run_id
    code, _ = run_program(spider_type, args=dict(args.spider_args), command_line=command_line, output=args.output)
    return code
