from __future__ import annotations

import datetime as dt
import os
import re
import socket
import stat
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import msgspec
import pytest

from pernix import job_dir
from pernix.errors import JobDirError
from pernix.job_dir import JobDir, RunRecord

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

STARTED = dt.datetime(2026, 10, 22, 8, 0, tzinfo=dt.UTC)
EARLIER_HOLDER = b"process 1 on elsewhere since 2026-10-21T08:00:00Z\n"
"""The lock file as a process that held the directory before left it."""


def _run(**changes: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "run_id": "r1",
        "spider": "books",
        "started_at": STARTED,
        "arguments": {"keyword": "dune"},
        "fingerprint": {"headers": [], "sort_query": True, "function": None},
        "pernix": "1.0",
    }
    return RunRecord(**(fields | changes))


def _taken(path: Path, run: RunRecord) -> RunRecord:
    """Take a job directory for a Run, then release it."""
    job = JobDir(path)
    try:
        return job.take(run)
    finally:
        job.release()


def _contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def test_a_new_run_is_recorded_in_a_directory_readable_by_its_owner_only(tmp_path: Path) -> None:
    job = JobDir(tmp_path / "jobs" / "books")

    record = job.take(_run())
    job.release()

    assert (record, job.resumed) == (_run(), False)
    assert sorted(path.name for path in job.path.iterdir()) == ["lock", "run.json"]
    assert stat.S_IMODE(job.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((job.path / "run.json").stat().st_mode) == 0o600
    assert msgspec.json.decode((job.path / "run.json").read_bytes(), type=RunRecord) == _run()


def test_an_existing_directory_keeps_its_permissions(tmp_path: Path) -> None:
    tmp_path.chmod(0o750)

    _taken(tmp_path, _run())

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o750


def test_the_run_recorded_is_resumed_as_it_was_left(tmp_path: Path) -> None:
    first = JobDir(tmp_path)
    first.take(_run())
    first.record_start_exhausted()
    first.release()
    later = JobDir(tmp_path)

    assert later.peek() == _run(start_exhausted=True)
    record = later.take(_run(started_at=STARTED + dt.timedelta(days=1), pernix="2.0"))
    later.release()

    assert (record, later.resumed) == (_run(start_exhausted=True), True)


def test_a_directory_another_process_holds_is_refused_naming_it_and_left_as_it_was(tmp_path: Path) -> None:
    holder = JobDir(tmp_path)
    holder.take(_run())
    before = _contents(tmp_path)

    with pytest.raises(JobDirError, match=rf"is used by process {os.getpid()} on .+ since "):
        _taken(tmp_path, _run())

    assert _contents(tmp_path) == before
    holder.release()
    _taken(tmp_path, _run())


HOLDS_UNTIL_KILLED = """
import sys, time
from pernix.job_dir import JobDir, RunRecord
import datetime as dt

JobDir(sys.argv[1]).take(RunRecord(run_id="r1", spider="books", started_at=dt.datetime.now(dt.UTC)))
print("taken", flush=True)
time.sleep(60)
"""


def test_a_process_killed_while_it_holds_the_directory_leaves_it_free(tmp_path: Path) -> None:
    with subprocess.Popen(  # noqa: S603  # this interpreter
        [sys.executable, "-c", HOLDS_UNTIL_KILLED, str(tmp_path)], stdout=subprocess.PIPE
    ) as holder:
        assert holder.stdout is not None
        assert holder.stdout.readline() == b"taken\n"
        with pytest.raises(JobDirError, match=rf"is used by process {holder.pid} "):
            _taken(tmp_path, _run())
        holder.kill()

    assert _taken(tmp_path, _run(arguments=None, fingerprint={})).run_id == "r1"


@pytest.mark.parametrize(
    ("changes", "problems"),
    [
        ({"run_id": "r2"}, ["run_id is 'r2', not 'r1'"]),
        ({"spider": "shop"}, ["the Spider is 'shop', not 'books'"]),
        (
            {"arguments": {"keyword": "emma", "pages": 3}},
            ["the Spider argument keyword is 'emma', not 'dune'", "the Spider argument pages is 3, not not given"],
        ),
        (
            {"fingerprint": {"headers": ["X-Lang"], "sort_query": True, "function": None}},
            ["fingerprint.headers is ['X-Lang'], not []"],
        ),
    ],
)
def test_a_run_that_does_not_continue_the_one_recorded_is_refused_with_what_differs(
    tmp_path: Path, changes: dict[str, Any], problems: list[str]
) -> None:
    _taken(tmp_path, _run())
    (tmp_path / "lock").write_bytes(EARLIER_HOLDER)
    before = _contents(tmp_path)

    with pytest.raises(JobDirError) as raised:
        _taken(tmp_path, _run(**changes))

    assert str(raised.value).splitlines() == [
        f"the job directory {tmp_path} holds the Run 'r1' of the Spider 'books', which this Run does not continue:",
        *(f"  - {problem}" for problem in problems),
    ]
    assert _contents(tmp_path) == before
    _taken(tmp_path, _run())  # the refused Run left it free


def test_arguments_that_are_not_known_are_not_compared(tmp_path: Path) -> None:
    _taken(tmp_path, _run(arguments=None))

    assert _taken(tmp_path, _run()).arguments is None


def test_a_frontier_without_a_record_is_refused(tmp_path: Path) -> None:
    (tmp_path / "frontier.db").write_bytes(b"")

    with pytest.raises(JobDirError, match=r"holds a Frontier, but no record of its Run in run\.json"):
        _taken(tmp_path, _run())


def test_a_record_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    (tmp_path / "run.json").write_text('{"run_id": 1}')
    job = JobDir(tmp_path)

    assert job.peek() is None
    with pytest.raises(JobDirError, match=r"the record of the Run in .+run\.json cannot be read: "):
        _taken(tmp_path, _run())


def _take(path: Path) -> None:
    _taken(path, _run())


def _take_recorded(path: Path) -> None:
    job = JobDir(path)
    try:
        job.take_recorded()
    finally:
        job.release()


@pytest.mark.parametrize("lock", [None, EARLIER_HOLDER], ids=["without a lock file", "with an earlier holder's"])
@pytest.mark.parametrize(
    ("files", "take"),
    [
        ({"frontier.db": b""}, _take),
        ({"run.json": b'{"run_id": 1}'}, _take),
        ({"run.json": msgspec.json.encode(_run(run_id="r0"))}, _take),
        ({"frontier.db": b""}, _take_recorded),
        ({"run.json": b'{"run_id": 1}'}, _take_recorded),
    ],
    ids=[
        "a Frontier but no record",
        "a record that cannot be read",
        "another Run",
        "a command, no record",
        "a command, a record that cannot be read",
    ],
)
def test_a_take_refused_for_what_the_directory_holds_leaves_every_file_as_it_was_creating_none(
    tmp_path: Path, files: dict[str, bytes], take: Callable[[Path], object], lock: bytes | None
) -> None:
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    if lock is not None:
        (tmp_path / "lock").write_bytes(lock)
    before = _contents(tmp_path)

    with pytest.raises(JobDirError):
        take(tmp_path)

    assert _contents(tmp_path) == before


def test_a_run_recorded_meanwhile_is_checked_again_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = job_dir._locked
    left: dict[str, bytes] = {}

    def taken_meanwhile(path: Path) -> int:
        """Let another process take the directory between the check of this one and its lock."""
        monkeypatch.setattr(job_dir, "_locked", locked)
        _taken(tmp_path, _run(run_id="r0"))
        left.update(_contents(tmp_path))
        return locked(path)

    monkeypatch.setattr(job_dir, "_locked", taken_meanwhile)

    with pytest.raises(JobDirError, match=r"holds the Run 'r0' of the Spider 'books', which this Run"):
        _taken(tmp_path, _run())

    assert _contents(tmp_path) == left


@pytest.mark.parametrize("take", [lambda job: job.take(_run()), JobDir.take_recorded], ids=["a Run", "a command"])
def test_a_process_refused_names_the_one_that_took_the_directory_since_another(
    tmp_path: Path, take: Callable[[JobDir], object]
) -> None:
    _taken(tmp_path, _run())
    (tmp_path / "lock").write_bytes(EARLIER_HOLDER)
    holder = JobDir(tmp_path)
    take(holder)

    try:
        with pytest.raises(
            JobDirError, match=rf"is used by process {os.getpid()} on {re.escape(socket.gethostname())} since "
        ):
            _taken(tmp_path, _run())
    finally:
        holder.release()


def test_a_directory_released_before_it_is_taken_is_not_taken(tmp_path: Path) -> None:
    job = JobDir(tmp_path)
    job.release()  # as a Run cancelled while another thread was about to take it

    with pytest.raises(RuntimeError, match="the job directory was released"):
        job.take(_run())

    assert _taken(tmp_path, _run()).run_id == "r1"  # no lock was left held


def test_the_control_file_is_looked_at_as_the_directory_is_taken(tmp_path: Path) -> None:
    without, written = JobDir(tmp_path / "without"), JobDir(tmp_path / "written")
    written.path.mkdir()
    (written.path / "control").write_text("drain")

    for job in (without, written):
        job.take(_run())
        job.release()

    assert written.control_path == tmp_path / "written" / "control"
    assert without.control_found is None
    assert written.control_found is not None
    status = written.control_path.stat()
    assert (written.control_found.st_ino, written.control_found.st_mtime_ns) == (status.st_ino, status.st_mtime_ns)
    assert "control" in [path.name for path in written.files]


def test_a_command_takes_the_directory_as_it_is_creating_nothing(tmp_path: Path) -> None:
    recorded = _run(start_exhausted=True, feeds={str((tmp_path / "books.jsonl").resolve()): "jsonl"})
    first = JobDir(tmp_path)
    first.take(_run())
    first.record_start_exhausted()
    first.record_feed(tmp_path / "books.jsonl", "jsonl")
    first.release()
    (tmp_path / "control").write_text("paused")
    before = _contents(tmp_path)
    job = JobDir(tmp_path)

    assert job.take_recorded() == recorded

    with pytest.raises(JobDirError, match=rf"is used by process {os.getpid()} "):
        _taken(tmp_path, _run())
    job.release()
    after = _contents(tmp_path)
    assert after.keys() == before.keys()
    assert {name: after[name] for name in ("run.json", "control")} == {
        name: before[name] for name in ("run.json", "control")
    }


def test_a_command_refuses_a_directory_that_holds_no_run_and_leaves_it_as_it_was(tmp_path: Path) -> None:
    (tmp_path / "frontier.db").write_bytes(b"")

    with pytest.raises(JobDirError, match=r"is not a job directory: it holds no record of a Run in run\.json"):
        JobDir(tmp_path).take_recorded()
    with pytest.raises(JobDirError, match="there is no job directory"):
        JobDir(tmp_path / "missing").take_recorded()

    assert sorted(path.name for path in tmp_path.iterdir()) == ["frontier.db"]


def test_a_command_refuses_a_directory_another_process_holds(tmp_path: Path) -> None:
    holder = JobDir(tmp_path)
    holder.take(_run())
    before = _contents(tmp_path)

    with pytest.raises(JobDirError, match=rf"is used by process {os.getpid()} on .+ since "):
        JobDir(tmp_path).take_recorded()

    assert _contents(tmp_path) == before
    holder.release()
