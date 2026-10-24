"""The job directory of a Run: where it keeps its state, so that a later process resumes it.

A Run with the `job_dir` setting keeps there its Frontier, a `SQLiteFrontier` in
`frontier.db`; a record of itself in `run.json`, which a later process checks before
continuing it; and `lock`, which one process at a time holds. The lock is an
advisory lock of the operating system, released when its process ends, however it
ends, so a process killed leaves none behind. Locks and SQLite's shared memory are
not reliable on network file systems, such as NFS or SMB, so the directory belongs
on a local one. A Platform may write `control` there, the state it wants the Run in,
which the Run reads by default; Pernix never writes it.

Whether a process holds the directory is told by testing its lock for an instant,
under a gate, a lock of its own, which a process taking the directory waits for
when it finds the lock held, so that the test never refuses it.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import socket
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import msgspec

from pernix.errors import JobDirError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

__all__ = ["Holder", "JobDir", "RunRecord"]

_RECORD = "run.json"
_FRONTIER = "frontier.db"
_LOCK = "lock"
_CONTROL = "control"
_FILES = (_RECORD, f".{_RECORD}.tmp", _FRONTIER, f"{_FRONTIER}-wal", f"{_FRONTIER}-shm", _LOCK, _CONTROL)
"""The files a job directory keeps, which no output of the Run may write."""


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
        feeds: The files the Feeds of the Run have begun, as resolved paths, with
            the name of their format, which a resumed Run continues.
    """

    run_id: str
    spider: str
    started_at: dt.datetime
    arguments: dict[str, Any] | None = None
    fingerprint: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])
    start_exhausted: bool = False
    pernix: str = ""
    feeds: dict[str, str] = msgspec.field(default_factory=dict[str, str])


_encoder = msgspec.json.Encoder()
_decoder = msgspec.json.Decoder(RunRecord)


@dataclass(frozen=True, slots=True)
class Holder:
    """The process that holds a job directory, as its lock file names it.

    Each is `None` when the lock file does not name it, as for an instant after a
    process took the directory, before it wrote its name.

    Attributes:
        pid: Its process id.
        host: The name of its host.
        since: When it took the directory, in UTC, to the second.
    """

    pid: int | None = None
    host: str | None = None
    since: dt.datetime | None = None


class JobDir:
    """A job directory, which one process at a time takes for its Run.

    It is taken once: once released, even while `take()` runs in another thread,
    it cannot be taken again.

    Attributes:
        path: The directory.
        record: The record of the Run, once taken.
        resumed: Whether the directory held the Run already when taken, so that
            this process resumes it.
        control_found: The control file as the directory held it when taken, as
            `os.stat()` tells, so that a stop written for an earlier process is told
            from one written for this one; `None` if it held none.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Name the directory; nothing is read or written yet.

        Args:
            path: The directory.
        """
        self.path = Path(path)
        self.record: RunRecord | None = None
        self.resumed = False
        self.control_found: os.stat_result | None = None
        self._lock: int | None = None
        self._released = False
        self._guard = threading.Lock()
        """Lets `release()` wait for a `take()` or a write in progress: no lock is left held, nothing written after."""

    @property
    def frontier_path(self) -> Path:
        """The database of the Run's Frontier."""
        return self.path / _FRONTIER

    @property
    def control_path(self) -> Path:
        """The control file, where a Platform writes the state it wants the Run in."""
        return self.path / _CONTROL

    @property
    def files(self) -> tuple[Path, ...]:
        """The files the directory keeps, which no output of the Run may write."""
        return tuple(self.path / name for name in _FILES)

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

    def read_record(self) -> RunRecord:
        """Read the Run recorded without taking the directory, also while a Run holds it; nothing is changed.

        The record is replaced at once when it changes, so it is read whole.

        Returns:
            The record.

        Raises:
            JobDirError: The directory does not exist, holds no record of a Run,
                or its record cannot be read.
        """
        if not self.path.is_dir():
            raise JobDirError(f"there is no job directory {self.path}")
        return self._recorded()

    def holder(self) -> Holder | None:
        """Tell which process holds the directory, without taking it; nothing is changed.

        The lock is tested for an instant, under the gate, so that a process taking
        the directory meanwhile waits for the test rather than being refused.

        Returns:
            The process, as the lock file names it, or `None` if no process holds
            the directory. For an instant after a process took the directory, the
            lock file names none, or still the process that held it before.
        """
        try:
            lock = os.open(self.path / _LOCK, os.O_RDONLY)
        except FileNotFoundError:
            return None  # a process makes it before it holds the directory
        try:
            with _gate(lock, self.path):
                if _locks(lock):
                    _unlock_file(lock)
                    return None
                return _holder(_holder_line(lock))
        finally:
            os.close(lock)

    def take_recorded(self) -> RunRecord:
        """Take the directory as it is, for a command on the Run recorded there: lock it, and read the record.

        Nothing is created or recorded, but for the lock file, which names this
        process, as `take()` has it; `control_found` stays `None`, as no Run takes it.

        Returns:
            The record.

        Raises:
            JobDirError: The directory does not exist, holds no record of a Run,
                its record cannot be read, or another process holds it. Nothing in
                it was changed.
            RuntimeError: The directory was released already.
        """
        with self._guard:
            if self._released:
                raise RuntimeError("the job directory was released")
            if not self.path.is_dir():
                raise JobDirError(f"there is no job directory {self.path}")
            found = self._lock_checked(self._recorded)
            self.record = found
            return found

    def take(self, run: RunRecord) -> RunRecord:
        """Take the directory for a Run: lock it, then record the Run, or check that it continues the one recorded.

        The directory is created if it is missing, readable by its owner only; an
        existing one keeps its permissions. A Run continues the one recorded if it
        has its `run_id` and its Spider, and, where both are known, its Spider
        arguments and the settings of its Fingerprints. As soon as the lock is held,
        the control file is looked at, for `control_found`. The lock file names this
        process once the directory is taken, not before, so that a Run refused
        leaves it as it was, or leaves none.

        Args:
            run: The Run of this process, as it would be recorded.

        Returns:
            The record: the one found if the Run is resumed, otherwise `run`.

        Raises:
            JobDirError: Another process holds the directory; it records another
                Run; it holds a Frontier but no record; or its record cannot be read.
                Nothing in it was changed.
            RuntimeError: The directory was released already.
        """
        with self._guard:
            if self._released:
                raise RuntimeError("the job directory was released")
            return self._take(run)

    def _take(self, run: RunRecord) -> RunRecord:
        self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Under the lock, the control file comes first, as anything written to it from then on is for this process.
        control, found = self._lock_checked(lambda: (_status(self.control_path), self._continued(run)))
        if found is not None:
            self.record, self.resumed, self.control_found = found, True, control
            return found
        try:
            self._write(run)
        except BaseException:
            self._unlock()
            raise
        self.record, self.resumed, self.control_found = run, False, control
        return run

    def record_start_exhausted(self) -> None:
        """Record that `Spider.start()` was iterated to its end, so that a resumed Run does not iterate it again.

        A `release()` in another thread waits for the record to be written.

        Raises:
            RuntimeError: The directory is not taken.
        """
        with self._guard:
            if self.record is None or self._lock is None:
                raise RuntimeError("the job directory is not taken")
            self.record = msgspec.structs.replace(self.record, start_exhausted=True)
            self._write(self.record)

    def recorded_feed(self, path: Path) -> str | None:
        """The name of the format a Feed of the Run began a file in, or None if none began it.

        Args:
            path: The file.

        Raises:
            RuntimeError: The directory is not taken.
        """
        if self.record is None or self._lock is None:
            raise RuntimeError("the job directory is not taken")
        return self.record.feeds.get(_feed_key(path))

    def record_feed(self, path: Path, format_name: str) -> None:
        """Record a file a Feed of the Run began, so that a resumed Run continues it.

        A `release()` in another thread waits for the record to be written.

        Args:
            path: The file.
            format_name: The name of the Feed's format.

        Raises:
            RuntimeError: The directory is not taken.
            OSError: The record cannot be written.
        """
        with self._guard:
            if self.record is None or self._lock is None:
                raise RuntimeError("the job directory is not taken")
            key = _feed_key(path)
            if self.record.feeds.get(key) == format_name:
                return
            feeds = {**self.record.feeds, key: format_name}
            self.record = msgspec.structs.replace(self.record, feeds=feeds)
            self._write(self.record)

    def release(self) -> None:
        """Release the lock, for another process to take the directory; releasing it again does nothing.

        A `take()` or a write of the record running in another thread is waited for, so that the lock it
        takes is released too, and nothing is written in the directory once it is released.
        """
        with self._guard:
            self._released = True
            self._unlock()

    def _unlock(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            os.close(lock)  # which releases the lock

    def _lock_checked[T](self, check: Callable[[], T]) -> T:
        """Lock the directory, check it with `check`, and only then write in the lock file that this process holds it.

        `check` reads the directory, raising `JobDirError` to refuse it, which is
        then left as it was, its lock file included. Without a lock file, which no
        process then holds, `check` runs before the lock too, so that a directory it
        refuses is not given one; it runs again under the lock, as another process
        may have taken the directory meanwhile.

        Returns:
            What `check` returns under the lock.

        Raises:
            JobDirError: Another process holds the directory, which the error names,
                or `check` refused it.
        """
        path = self.path / _LOCK
        if not path.exists():
            check()
        self._lock = _locked(path)
        try:
            checked = check()
            _name_holder(self._lock)
        except BaseException:
            self._unlock()
            raise
        return checked

    def _continued(self, run: RunRecord) -> RunRecord | None:
        """The record that `run` continues, or `None` if the directory holds no Run yet; nothing is changed.

        Raises:
            JobDirError: The directory records another Run, holds a Frontier but no
                record, or its record cannot be read.
        """
        found = self._read()
        if found is None:
            if self.frontier_path.exists():
                raise JobDirError(
                    f"the job directory {self.path} holds a Frontier, but no record of its Run in {_RECORD}"
                )
            return None
        problems = _differences(found, run)
        if problems:
            raise JobDirError(
                f"the job directory {self.path} holds the Run {found.run_id!r} of the Spider {found.spider!r}, "
                "which this Run does not continue:\n" + "\n".join(f"  - {problem}" for problem in problems)
            )
        return found

    def _recorded(self) -> RunRecord:
        """The record of the Run the directory holds, for a command on it; nothing is changed.

        Raises:
            JobDirError: The directory holds no record of a Run, or it cannot be read.
        """
        found = self._read() if (self.path / _RECORD).is_file() else None
        if found is None:
            raise JobDirError(f"{self.path} is not a job directory: it holds no record of a Run in {_RECORD}")
        return found

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


def _feed_key(path: Path) -> str:
    """A Feed's file as `run.json` records it: resolved, so that any path to the same file names it."""
    return str(path.resolve())


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


def _status(path: Path) -> os.stat_result | None:
    """The `os.stat()` of a file; `None` if there is none, or it cannot be read, which reading it tells."""
    try:
        return path.stat()
    except OSError:
        return None


def _locked(path: Path) -> int:
    """Open and lock the lock file, creating it if it is missing, but writing nothing in it.

    The lock found held is tried once more under the gate, as a process testing it
    holds it for an instant, under the gate, and so no longer once the gate is had.

    Raises:
        JobDirError: Another process holds it; the error names that process.
    """
    lock = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _locks(lock):
            with _gate(lock, path.parent):
                if not _locks(lock):
                    holder = _holder_line(lock) or "another process"
                    raise JobDirError(f"the job directory {path.parent} is used by {holder}")
    except BaseException:
        os.close(lock)
        raise
    return lock


def _locks(lock: int) -> bool:
    """Lock the lock file if no process holds it; whether it did."""
    try:
        _lock(lock)
    except OSError:
        return False
    return True


def _holder_line(lock: int) -> str:
    """The line the lock file holds, which names the process that last took the directory."""
    return os.read(lock, 512).decode(errors="replace").strip()


_HOLDER = re.compile(r"process (\d+) on (\S+) since (\S+)")


def _holder(line: str) -> Holder:
    """The process a line of the lock file names; nothing of it when the line does not name one."""
    named = _HOLDER.fullmatch(line)
    if named is None:
        return Holder()
    try:
        since = dt.datetime.strptime(named[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        since = None
    return Holder(int(named[1]), named[2], since)


def _name_holder(lock: int) -> None:
    """Write in the lock file held that this process holds it, for another process refused to name it."""
    since = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.ftruncate(lock, 0)
    os.write(lock, f"process {os.getpid()} on {socket.gethostname()} since {since}\n".encode())


if sys.platform == "win32":
    import msvcrt

    _LOCKED_BYTE = 1 << 30
    """The byte locked, beyond the text, which other processes can then read."""
    _GATE_BYTE = _LOCKED_BYTE + 1
    """The byte of the gate, which is held for an instant only."""

    def _lock(lock: int) -> None:
        _locking(lock, _LOCKED_BYTE, msvcrt.LK_NBLCK)

    def _unlock_file(lock: int) -> None:
        _locking(lock, _LOCKED_BYTE, msvcrt.LK_UNLCK)

    @contextmanager
    def _gate(lock: int, directory: Path) -> Generator[None]:
        """Hold the gate: a byte of the lock file, waited for, as msvcrt.locking() waits, ten seconds at most."""
        _locking(lock, _GATE_BYTE, msvcrt.LK_LOCK)
        try:
            yield
        finally:
            _locking(lock, _GATE_BYTE, msvcrt.LK_UNLCK)

    def _locking(lock: int, byte: int, mode: int) -> None:
        """Lock or unlock a byte of the lock file, leaving it read from its start."""
        os.lseek(lock, byte, os.SEEK_SET)
        try:
            msvcrt.locking(lock, mode, 1)
        finally:
            os.lseek(lock, 0, os.SEEK_SET)

else:
    import fcntl

    def _lock(lock: int) -> None:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(lock: int) -> None:
        fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def _gate(lock: int, directory: Path) -> Generator[None]:
        """Hold the gate: a lock of the directory itself, waited for, as its holders hold it for an instant only."""
        gate = os.open(directory, os.O_RDONLY)
        try:
            fcntl.flock(gate, fcntl.LOCK_EX)
            yield
        finally:
            os.close(gate)  # which releases it
