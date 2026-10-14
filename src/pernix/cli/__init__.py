"""The `pernix` command: `run`, `fetch`, `shell`, and `check`."""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version
from typing import TYPE_CHECKING

from pernix.cli import check_command, fetch_command, run_command, shell_command
from pernix.cli.options import UsageError
from pernix.errors import CONFIGURATION_ERRORS
from pernix.runner import ExitCode

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `pernix` command.

    Results go to stdout, and messages and logs to stderr. A configuration error,
    such as an invalid setting or a Spider that cannot be found, is printed without
    a traceback and exits with 2, as do invalid command-line options.

    Args:
        argv: The arguments after the program name; defaults to `sys.argv[1:]`.

    Returns:
        The exit status.
    """
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (UsageError, *CONFIGURATION_ERRORS) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return ExitCode.CONFIGURATION_ERROR
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pernix", description="Run Spiders, fetch pages, and check a Pernix project.")
    parser.add_argument("--version", action="version", version=f"pernix {version('pernix')}")
    commands = parser.add_subparsers(title="commands", metavar="COMMAND", required=True)
    for command in (run_command, fetch_command, shell_command, check_command):
        command.add_parser(commands)
    return parser
