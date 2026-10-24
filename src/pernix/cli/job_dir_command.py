"""`pernix job-dir`: the Run a job directory keeps, its Dead Requests, and reviving them."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import orjson

from pernix.cli.closed_stdout import discard_stdout
from pernix.cli.options import UsageError
from pernix.errors import JobDirError, SerializationError
from pernix.frontiers import FrontierCounts, SQLiteFrontier
from pernix.frontiers.sqlite_frontier import FrontierReader
from pernix.job_dir import JobDir
from pernix.logs import Redactor
from pernix.runner import ExitCode
from pernix.spider import Spider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Generator

    from pernix.frontiers import DeadRequest
    from pernix.job_dir import Holder, RunRecord

__all__ = ["SCHEMA", "add_parser"]

SCHEMA = 1
"""The version of the objects `pernix job-dir --json` writes, raised when a field changes meaning or goes away."""

_REDACTOR = Redactor()
"""Masks the credentials and the values of sensitive names in the URLs and reasons listed, as the built-in rules of
the logs find them; the names of `log.redact` and the `Secret` settings are not known, as no settings are loaded."""


class _StandIn(Spider):
    """Stands in for the Spider of the Run, as describing and reviving Dead Requests restores none."""

    name = "pernix"


@dataclass(frozen=True, slots=True)
class _Selection:
    """Which Dead Requests a command picks: any of the values of each filter given, and every filter given.

    Attributes:
        errors: The types of the errors they failed with, as `DeadRequest.error`.
        reasons: Text their reasons hold.
        callbacks: The names of their Callbacks; `parse` also picks those without one.
    """

    errors: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    callbacks: tuple[str, ...] = ()

    @property
    def everything(self) -> bool:
        """Whether no filter is given, so that every Dead Request is picked."""
        return not (self.errors or self.reasons or self.callbacks)

    def picks(self, dead: DeadRequest) -> bool:
        """Tell whether a Dead Request is picked.

        Args:
            dead: The Dead Request.

        Returns:
            Whether it matches every filter given.
        """
        return (
            (not self.errors or dead.error in self.errors)
            and (not self.reasons or any(text in dead.reason for text in self.reasons))
            and (not self.callbacks or (dead.callback or "parse") in self.callbacks)
        )


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """Register `pernix job-dir` and its commands."""
    parser = commands.add_parser(
        "job-dir",
        help="inspect the Run a job directory keeps, and revive its Dead Requests",
        description=(
            "Inspect the Run a job directory keeps, list its Dead Requests, and revive them, so that the next Run "
            "resumed from the directory fetches them again. status and dead read the directory as it is, also "
            "while a Run uses it, changing nothing. revive takes the directory's lock, so it refuses a directory a "
            "Run is using, and opening the Frontier then brings its database to the schema of this Pernix, and "
            "makes the Requests whose Leases expired ready, counting an attempt, as a resumed Run would. No "
            "command deletes anything."
        ),
    )
    job_commands = parser.add_subparsers(title="commands", metavar="COMMAND", required=True)
    status = job_commands.add_parser(
        "status",
        parents=[_directory_parser()],
        help="print the Run, the process holding the directory, and how many of its Requests are in each state",
        description=(
            "Print the Run the job directory records, its run_id, Spider, and first start, the process that holds "
            "the directory, if any, and how many of its Requests are ready, delayed, leased, done, and dead, as "
            "they are stored: while a Run holds the directory, as they are at that moment. Exits with 0, and with "
            "2 when the directory is not a job directory, or this Pernix cannot read its Frontier."
        ),
    )
    status.add_argument("--json", action="store_true", help="print it as one JSON object")
    status.set_defaults(handler=run_status)
    dead = job_commands.add_parser(
        "dead",
        parents=[_directory_parser(), _filters_parser()],
        help="list the Dead Requests",
        description=(
            "List the Dead Requests, oldest first, one per line: its id, attempts, Callback, URL, and reason, "
            "separated by tabs; with --json, one JSON object per line. Credentials in URLs and the values of "
            "sensitive names, such as token or password, are masked as in the logs, though not the names of "
            "log.redact or the values of Secret settings, as no settings are loaded. Filters pick some, matching "
            "the reasons unmasked: any of the values given for a filter, and every filter given, must match. While "
            "a Run holds the directory, they are read a few hundred at a time, each as it then is. Exits with 0, 1 "
            "when stdout was closed before the end, and 2 as status does."
        ),
    )
    dead.add_argument("--json", action="store_true", help="print each Dead Request as one JSON object")
    dead.set_defaults(handler=run_dead)
    revive = job_commands.add_parser(
        "revive",
        parents=[_directory_parser(), _filters_parser()],
        help="make Dead Requests ready again, for the next Run resumed from the directory",
        description=(
            "Make the Dead Requests the filters pick, or all of them with --all, ready again, with their attempts "
            "and retry budgets reset, so that the next Run resumed from the directory fetches them, counting "
            "their attempts from 1; they keep their reasons until then. dead with the same filters lists them. "
            "Exits with 0, and with 2 as status does, when a Run is using the directory, or with neither filters "
            "nor --all, or both."
        ),
    )
    revive.add_argument("--all", action="store_true", help="revive every Dead Request; needed without filters")
    revive.set_defaults(handler=run_revive)


def run_status(args: argparse.Namespace) -> int:
    """Print the Run of the job directory, the process holding it, and how many of its Requests are in each state.

    Nothing in the directory is changed, and a Run holding it goes on.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        pernix.errors.JobDirError: The directory is not a job directory, or its
            Frontier cannot be read by this Pernix.
    """
    job = JobDir(args.directory)
    record = job.read_record()
    holder = job.holder()
    with _read(job) as frontier:
        counts = FrontierCounts(0, 0, 0, 0, 0) if frontier is None else frontier.counts()
    if args.json:
        sys.stdout.write(orjson.dumps(_described_run(record, holder, counts)).decode() + "\n")
        return ExitCode.COMPLETED
    start = "iterated to its end" if record.start_exhausted else "not iterated to its end; a resumed Run iterates it"
    lines = [
        f"Run: {record.run_id}",
        f"Spider: {record.spider}",
        f"First started: {_timestamp(record.started_at)}",
        f"Started by: Pernix {record.pernix or '(version unknown)'}",
        f"start(): {start}",
        f"Held by: {_holder_shown(holder)}",
        f"Requests: {counts.ready} ready, {counts.delayed} delayed, {counts.leased} leased, {counts.done} done, "
        f"{counts.dead} dead",
    ]
    if holder is not None:
        lines.append("Counts: as they are at this moment; the Run holding the directory goes on changing them")
    elif counts.leased:
        lines.append(
            f"Leased: {counts.leased}, left in flight by a process that ended; the next Run makes them ready again, "
            "each counting an attempt"
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return ExitCode.COMPLETED


def run_dead(args: argparse.Namespace) -> int:
    """List the Dead Requests of the job directory the filters pick.

    Nothing in the directory is changed, and a Run holding it goes on.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        pernix.errors.JobDirError: The directory is not a job directory, or its
            Frontier cannot be read by this Pernix.
    """
    selection = _selection(args)
    job = JobDir(args.directory)
    job.read_record()  # which tells a directory that is not a job directory
    with _read(job) as frontier:
        listed = frontier is None or _list(frontier, selection, json=args.json)
    return ExitCode.COMPLETED if listed else ExitCode.FAILED


def run_revive(args: argparse.Namespace) -> int:
    """Revive the Dead Requests of the job directory the filters pick, or all of them.

    Args:
        args: The parsed command line.

    Returns:
        The exit status.

    Raises:
        UsageError: Neither filters nor `--all` are given, or both are.
        pernix.errors.JobDirError: The directory is not a job directory, another
            process holds it, or its Frontier cannot be read by this Pernix.
    """
    selection = _selection(args)
    if selection.everything and not args.all:
        raise UsageError("give filters (--error, --reason, or --callback) for the Dead Requests to revive, or --all")
    if args.all and not selection.everything:
        raise UsageError("--all revives every Dead Request; give it without filters")
    with _taken(args.directory) as (job, _):
        revived, dead = asyncio.run(_revive(job, selection))
    sys.stderr.write(f"Revived Dead Requests in {job.path}: {revived} of {dead}")
    sys.stderr.write("; the next Run resumed from it fetches them.\n" if revived else ".\n")
    return ExitCode.COMPLETED


def _directory_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("directory", metavar="DIR", help="the job directory, as given to pernix run --job-dir")
    return parser


def _filters_parser() -> argparse.ArgumentParser:
    """The filters of the commands that pick Dead Requests, each repeatable."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--error",
        dest="errors",
        metavar="TYPE",
        action="append",
        default=[],
        help="those that failed with an error of this type, such as KeyError or RetriesExhausted",
    )
    parser.add_argument(
        "--reason",
        dest="reasons",
        metavar="TEXT",
        action="append",
        default=[],
        help="those whose reason holds TEXT, such as 'HTTP 503'",
    )
    parser.add_argument(
        "--callback",
        dest="callbacks",
        metavar="NAME",
        action="append",
        default=[],
        help="those whose Callback is the Spider method NAME; parse also picks those without one",
    )
    return parser


def _selection(args: argparse.Namespace) -> _Selection:
    return _Selection(errors=tuple(args.errors), reasons=tuple(args.reasons), callbacks=tuple(args.callbacks))


@contextmanager
def _taken(directory: str) -> Generator[tuple[JobDir, RunRecord]]:
    """Take a job directory as it is, releasing it once done."""
    job = JobDir(directory)
    record = job.take_recorded()
    try:
        yield job, record
    finally:
        job.release()


@asynccontextmanager
async def _frontier(job: JobDir) -> AsyncGenerator[SQLiteFrontier | None]:
    """Open the Frontier of a job directory, or give `None` when it has none, which is then not created."""
    if not job.frontier_path.exists():
        yield None
        return
    frontier = SQLiteFrontier(job.frontier_path, _StandIn())
    try:
        yield frontier
    except SerializationError as exc:  # the database is not one this Pernix reads
        raise JobDirError(str(exc)) from exc
    finally:
        await frontier.close()


@contextmanager
def _read(job: JobDir) -> Generator[FrontierReader | None]:
    """Read the Frontier of a job directory as it is, or give `None` when it has none, which is then not created."""
    if not job.frontier_path.exists():
        yield None
        return
    try:
        yield FrontierReader(job.frontier_path)
    except SerializationError as exc:  # the database is not one this Pernix reads
        raise JobDirError(str(exc)) from exc


def _list(frontier: FrontierReader, selection: _Selection, *, json: bool) -> bool:
    """Write the Dead Requests picked to stdout; whether stdout stayed open until the end."""
    try:
        for dead in frontier.dead_requests():
            if selection.picks(dead):
                sys.stdout.write(_json_line(dead) if json else _text_line(dead))
        sys.stdout.flush()
    except BrokenPipeError:
        discard_stdout()
        return False
    return True


async def _revive(job: JobDir, selection: _Selection) -> tuple[int, int]:
    """Revive the Dead Requests picked; how many were, and how many were dead."""
    async with _frontier(job) as frontier:
        if frontier is None:
            return 0, 0
        total = (await frontier.counts()).dead
        picked = [dead.id async for dead in frontier.dead_requests() if selection.picks(dead)]
        return await frontier.revive(picked), total


def _holder_shown(holder: Holder | None) -> str:
    if holder is None:
        return "no process"
    if holder.pid is None:
        return "another process"
    since = "" if holder.since is None else f" since {_timestamp(holder.since)}"
    return f"process {holder.pid} on {holder.host}{since}"


def _described_run(record: RunRecord, holder: Holder | None, counts: FrontierCounts) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "run_id": record.run_id,
        "spider": record.spider,
        "started_at": _timestamp(record.started_at),
        "start_exhausted": record.start_exhausted,
        "pernix": record.pernix,
        "holder": None if holder is None else _described_holder(holder),
        "requests": {
            "ready": counts.ready,
            "delayed": counts.delayed,
            "leased": counts.leased,
            "done": counts.done,
            "dead": counts.dead,
        },
    }


def _described_holder(holder: Holder) -> dict[str, Any]:
    since = None if holder.since is None else _timestamp(holder.since)
    return {"pid": holder.pid, "host": holder.host, "since": since}


def _json_line(dead: DeadRequest) -> str:
    described = {
        "schema": SCHEMA,
        "id": dead.id,
        "url": _REDACTOR.redact(dead.url),
        "error": _REDACTOR.redact(dead.error),
        "reason": _REDACTOR.redact(dead.reason),
        "attempts": dead.attempts,
        "spent": dict(dead.spent),
        "callback": dead.callback,
        "errback": dead.errback,
        "rendered": dead.rendered,
    }
    return orjson.dumps(described).decode() + "\n"


def _text_line(dead: DeadRequest) -> str:
    # Masked first, as a header line is masked to its end, then on one line for each Dead Request.
    reason = " ".join(_REDACTOR.redact(dead.reason).splitlines()).replace("\t", " ")
    return f"{dead.id}\t{dead.attempts}\t{dead.callback or 'parse'}\t{_REDACTOR.redact(dead.url)}\t{reason}\n"


def _timestamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds")
