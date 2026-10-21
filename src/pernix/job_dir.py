"""The job directory of a Run: where it keeps its state, so that a later process resumes it.

A Run with the `job_dir` setting keeps there its Frontier, a `SQLiteFrontier` in
`frontier.db`; a record of itself in `run.json`, which a later process checks before
continuing it; and `lock`, which one process at a time holds. The lock is an
advisory lock of the operating system, released when its process ends, however it
ends, so a process killed leaves none behind. Locks and SQLite's shared memory are
not reliable on network file systems, such as NFS or SMB, so the directory belongs
on a local one.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import sys
from pathlib import Path
from typing import Any

import msgspec

from pernix.errors import JobDirError

__all__ = ["JobDir", "RunRecord"]

_RECORD = "run.json"
_FRONTIER = "frontier.db"
_LOCK = "lock"


class RunRecord(msgspec.Struct, kw_only=True, frozen=True):
    """What a job directory records of its Run, in `run.json`.

    Attributes:
        run_id: The `run_id` of the Run.
        spider: The name of its Spider.
        started_at: When it first started, in UTC.
        arguments: The Spider arguments it was given, as converted for the Spider;
            `None` when they are not known, as for a Crawler created with a Spider.
        fingerprint: The settings of its Fingerprints: `headers`, `sort_query`, and
            `function`, named by its import path.
        start_exhausted: Whether `Spider.start()` was iterated to its end.
        pernix: The version of Pernix that started the Run.
    """

    run_id: str
    spider: str
    started_at: dt.datetime
    arguments: dict[str, Any] | None = None
    fingerprint: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])
    start_exhausted: bool = False
    pernix: str = ""


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(RunRecord)


class JobDir:
    """A job directory, which one process at a time takes for its Run.

    Attributes:
        path: The directory.
        record: The record of the Run, once taken.
        resumed: Whether the directory held the Run already when taken, so that
            this process resumes it.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Name the directory; nothing is read or written yet.

        Args:
            path: The directory.
        """
        self.path = Path(path)
        self.record: RunRecord | None = None
        self.resumed = False
        self._lock: int | None = None

    @property
    def frontier_path(self) -> Path:
        """The database of the Run's Frontier."""
        return self.path / _FRONTIER

    def peek(self) -> RunRecord | None:
        """Read the Run recorded without taking the directory, as a guess for what `take()` will find.

        Returns:
            The record, or `None` if there is none, or it cannot be read, which
            `take()` then tells.
        """
        try:
            return self._read()
        except (OSError, JobDirError):
            return None

    def take(self, run: RunRecord) -> RunRecord:
        """Take the directory for a Run: lock it, then record the Run, or check that it continues the one recorded.

        The directory is created if it is missing, readable by its owner only; an
        existing one keeps its permissions. A Run continues the one recorded if it
        has its `run_id` and its Spider, and, where both are known, its Spider
        arguments and the settings of its Fingerprints.

        Args:
            run: The Run of this process, as it would be recorded.

        Returns:
            The record: the one found if the Run is resumed, otherwise `run`.

        Raises:
            JobDirError: Another process holds the directory; it records another
                Run; it holds a Frontier but no record; or its record cannot be read.
                Nothing in it was changed.
        """
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = _locked(self.path / _LOCK)
        try:
            found = self._read()
            if found is None:
                if self.frontier_path.exists():
                    raise JobDirError(
                        f"the job directory {self.path} holds a Frontier, but no record of its Run in {_RECORD}"
                    )
                self._write(run)
                self.record, self.resumed = run, False
                return run
            problems = _differences(found, run)
            if problems:
                raise JobDirError(
                    f"the job directory {self.path} holds the Run {found.run_id!r} of the Spider {found.spider!r}, "
                    "which this Run does not continue:\n" + "\n".join(f"  - {problem}" for problem in problems)
                )
            self.record, self.resumed = found, True
            return found
        except BaseException:
            self.release()
            raise

    def record_start_exhausted(self) -> None:
        """Record that `Spider.start()` was iterated to its end, so that a resumed Run does not iterate it again.

        Raises:
            RuntimeError: The directory is not taken.
        """
        if self.record is None or self._lock is None:
            raise RuntimeError("the job directory is not taken")
        self.record = msgspec.structs.replace(self.record, start_exhausted=True)
        self._write(self.record)

    def release(self) -> None:
        """Release the lock, for another process to take the directory; releasing it again does nothing."""
        lock, self._lock = self._lock, None
        if lock is not None:
            os.close(lock)  # which releases the lock

    def _read(self) -> RunRecord | None:
        try:
            data = (self.path / _RECORD).read_bytes()
        except FileNotFoundError:
            return None
        try:
            return _decoder.decode(data)
        except msgspec.MsgspecError as exc:
            raise JobDirError(f"the record of the Run in {self.path / _RECORD} cannot be read: {exc}") from exc

    def _write(self, record: RunRecord) -> None:
        """Replace the record at once, so that it is whole or not changed, readable by the owner only."""
        target = self.path / _RECORD
        partial = target.with_name(f".{_RECORD}.tmp")
        file = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(file, _encoder.encode(record) + b"\n")
            os.fsync(file)
        finally:
            os.close(file)
        partial.replace(target)


def _differences(found: RunRecord, run: RunRecord) -> list[str]:
    """What tells a Run apart from the one recorded, one line each."""
    problems: list[str] = []
    if run.run_id != found.run_id:
        problems.append(f"run_id is {run.run_id!r}, not {found.run_id!r}")
    if run.spider != found.spider:
        problems.append(f"the Spider is {run.spider!r}, not {found.spider!r}")
    if run.arguments is not None and found.arguments is not None:
        for name in sorted(run.arguments.keys() | found.arguments.keys()):
            now, before = run.arguments.get(name, _MISSING), found.arguments.get(name, _MISSING)
            if now != before:
                problems.append(f"the Spider argument {name} is {_shown(now)}, not {_shown(before)}")
    for name in sorted(run.fingerprint.keys() | found.fingerprint.keys()):
        now, before = run.fingerprint.get(name, _MISSING), found.fingerprint.get(name, _MISSING)
        if now != before:
            problems.append(f"fingerprint.{name} is {_shown(now)}, not {_shown(before)}")
    return problems


_MISSING = object()


def _shown(value: object) -> str:
    return "not given" if value is _MISSING else repr(value)


def _locked(path: Path) -> int:
    """Open and lock the lock file, writing who holds it.

    Raises:
        JobDirError: Another process holds it; the error names that process.
    """
    lock = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock(lock)
    except OSError:
        holder = os.read(lock, 512).decode(errors="replace").strip() or "another process"
        os.close(lock)
        raise JobDirError(f"the job directory {path.parent} is used by {holder}") from None
    except BaseException:
        os.close(lock)
        raise
    since = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.ftruncate(lock, 0)
    os.write(lock, f"process {os.getpid()} on {socket.gethostname()} since {since}\n".encode())
    return lock


if sys.platform == "win32":
    import msvcrt

    _LOCKED_BYTE = 1 << 30
    """The byte locked, beyond the text, which other processes can then read."""

    def _lock(lock: int) -> None:
        os.lseek(lock, _LOCKED_BYTE, os.SEEK_SET)
        try:
            msvcrt.locking(lock, msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(lock, 0, os.SEEK_SET)

else:
    import fcntl

    def _lock(lock: int) -> None:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
