from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from pernix.crawler import Crawler
from pernix.spider import Spider

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["UsageError", "command_crawler", "key_value", "settings_parser", "spider_arguments_parser"]


class UsageError(Exception):
    """A command was given something it cannot use, such as a Spider that does not exist; it exits with 2."""


class _CommandSpider(Spider):
    """Stands in for a Spider in the commands that run none, so that components can be built."""

    name = "pernix"


def key_value(text: str) -> tuple[str, str]:
    """Parse a `KEY=VALUE` option.

    Args:
        text: The option's value.

    Returns:
        The key and the value, split at the first `=`.

    Raises:
        argparse.ArgumentTypeError: There is no `=`, or the key is empty.
    """
    key, sep, value = text.partition("=")
    if not sep or not key.strip():
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key.strip(), value


def settings_parser() -> argparse.ArgumentParser:
    """The `-s KEY=VALUE` option every command takes."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "-s",
        "--set",
        dest="settings",
        metavar="KEY=VALUE",
        type=key_value,
        action="append",
        default=[],
        help="a setting, such as concurrency=4; values are parsed like PERNIX_* environment variables",
    )
    return parser


def spider_arguments_parser() -> argparse.ArgumentParser:
    """The `-a KEY=VALUE` option of the commands that take a Spider."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "-a",
        "--arg",
        dest="spider_args",
        metavar="KEY=VALUE",
        type=key_value,
        action="append",
        default=[],
        help="a Spider argument, converted to the type its __init__ parameter declares",
    )
    return parser


def command_crawler(
    settings: Sequence[tuple[str, str]],
    spider_type: type[Spider] | None = None,
    spider_args: Sequence[tuple[str, str]] = (),
) -> Crawler:
    """Load the settings of a command, with its `-s` options, and create a Crawler.

    Args:
        settings: The `-s` options; a later one replaces an earlier one.
        spider_type: The Spider, or `None` for a stand-in in commands that run none.
        spider_args: The `-a` options; a later one replaces an earlier one.

    Returns:
        The Crawler, not started.

    Raises:
        pernix.errors.SettingsError: The settings are invalid.
        pernix.errors.AddonError: An enabled Addon cannot be loaded.
        pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
    """
    return Crawler.from_spider(spider_type or _CommandSpider, args=dict(spider_args), command_line=dict(settings))
