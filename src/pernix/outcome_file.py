from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
import secrets
from typing import TYPE_CHECKING, Any, cast

import orjson

from pernix.exit_code import ExitCode
from pernix.feeds.feed import render_path

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from pernix.crawler import Crawler
    from pernix.logs import Redactor
    from pernix.stats import StatValue

__all__ = ["SCHEMA", "OutcomeFile"]

logger = logging.getLogger(__name__)

SCHEMA = 1
"""The version of the outcome document, raised when a field changes meaning or goes away."""

_JSON_INTEGERS = range(-(2**63), 2**64)
"""The integers written as JSON numbers; larger stats are written as strings."""


class OutcomeFile:
    """Writes the outcome file of a Run: a JSON document telling a Platform how the Run ended.

    The document is an object of `schema` (1); `run_id`; `spider`, the Spider's
    name; `reason`, how the Run ended: `finished`, `drained`, `forced`, `failed`, or
    `cancelled`; `exit_code`, the one `run()` and `pernix run` exit with for that
    ending, also for a Run inside an application; `started_at` and `finished_at`, in
    UTC, ISO 8601; `error`, for a failed Run, the `type` and `message` of the error
    it raised, or of the only one an exception group holds, whose `message`
    otherwise lists them, and `null` for any other reason; and `stats`, the stats as
    the Run ends, by dotted key. The message and the stats are masked as the Run's
    log records are.

    The document is written under a temporary name next to the file, synced to disk,
    and renamed over it, so the file appears complete or not at all, replacing any
    existing one. Missing parent directories are created. A failure to write it is
    logged, and changes nothing else.
    """

    def __init__(self, crawler: Crawler, template: str) -> None:
        """Configure the outcome file of a Run.

        Args:
            crawler: The Crawler of the Run.
            template: The path, which may contain `{name}`, `{run_id}`, and `{time}`,
                as Feed paths do.
        """
        self._crawler = crawler
        self._template = template

    def write(
        self,
        reason: str,
        *,
        error: BaseException | None = None,
        interrupted: bool = False,
        redactor: Redactor,
    ) -> None:
        """Write how the Run ended, as its last step.

        Args:
            reason: How the Run ended.
            error: What ended it, when it failed or was cancelled.
            interrupted: Whether a stop signal stopped it.
            redactor: Masks the error message and the stats.
        """
        crawler = self._crawler
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": crawler.run_id,
            "spider": crawler.spider.name,
            "reason": reason,
            "exit_code": int(ExitCode.of(error, interrupted=interrupted)),
            "started_at": _timestamp(crawler.started_at),
            "finished_at": _timestamp(dt.datetime.now(dt.UTC)),
            "error": _described(error, redactor) if error is not None and reason == "failed" else None,
            "stats": _masked_stats(crawler.stats.snapshot(), redactor),
        }
        try:
            path = render_path(
                self._template, name=crawler.spider.name, run_id=crawler.run_id, started=crawler.started_at
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("Cannot write the outcome file: invalid path template %r: %s", self._template, exc)
            return
        try:
            _replace(path, orjson.dumps(document, option=orjson.OPT_INDENT_2) + b"\n")
        except OSError as exc:
            logger.error("Cannot write the outcome file %s: %s", path, exc)


def _replace(path: Path, content: bytes) -> None:
    """Write a file under a temporary name next to it, sync it, and rename it over the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
    try:
        with partial.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        partial.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise


def _timestamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds")


def _described(error: BaseException, redactor: Redactor) -> dict[str, str]:
    """The type and masked message of an error, or of the only one a group holds; a group of several lists them."""
    leaves = _leaves(error)
    if len(leaves) == 1:
        [error] = leaves
        message = str(error)
    else:
        message = "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves)
    return {"type": type(error).__name__, "message": redactor.redact(message)}


def _leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        group = cast("BaseExceptionGroup[BaseException]", error)
        return [leaf for inner in group.exceptions for leaf in _leaves(inner)]
    return [error]


def _masked_stats(stats: Mapping[str, StatValue], redactor: Redactor) -> dict[str, StatValue]:
    masked: dict[str, StatValue] = {}
    for key, value in stats.items():
        if isinstance(value, str):
            value = redactor.redact(value)
        elif isinstance(value, int) and value not in _JSON_INTEGERS:
            value = str(value)
        masked[redactor.redact(key)] = value
    return masked
