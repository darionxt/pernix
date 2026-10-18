from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

from pernix.fetchers.launched_browser import require_patchright
from pernix.runner import ExitCode

if TYPE_CHECKING:
    import argparse

__all__ = ["add_parser"]

_ENGINE = ("--no-shell", "chromium")
"""What patchright installs for the browser Fetcher: Chrome for Testing, which it launches, without the headless shell."""


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix browser` and its commands."""
    parser = commands.add_parser(
        "browser",
        help="manage the browser engine of the browser Fetcher",
        description="Manage the browser engine the browser Fetcher renders pages with.",
    )
    browser_commands = parser.add_subparsers(title="commands", metavar="COMMAND", required=True)
    install = browser_commands.add_parser(
        "install",
        help="install the browser engine",
        description=(
            "Install the browser engine the browser Fetcher launches, Chrome for Testing, with patchright, which "
            "pernix[browser] installs. An engine already installed is kept. Exits with 0 when the engine is "
            "installed, 1 when installing it failed, and 2 when patchright is not installed."
        ),
    )
    install.add_argument(
        "--with-deps", action="store_true", help="also install the system libraries the browser needs, on Linux"
    )
    install.set_defaults(handler=run_install)


def run_install(args: argparse.Namespace) -> int:
    """Install the browser engine with patchright.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        pernix.errors.ComponentError: patchright is not installed.
    """
    require_patchright()
    deps = ("--with-deps",) if args.with_deps else ()
    completed = subprocess.run([sys.executable, "-m", "patchright", "install", *deps, *_ENGINE], check=False)  # noqa: S603  # patchright's own command
    if completed.returncode != 0:
        sys.stderr.write(
            f"Error: installing the browser engine failed: patchright exited with {completed.returncode}\n"
        )
        return ExitCode.FAILED
    sys.stderr.write("The browser engine is installed.\n")
    return ExitCode.COMPLETED
