from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import pytest

from pernix.control import ControlFile, read_control_file

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.control import ControlState


def _write(path: Path, state: str) -> None:
    """Write a control file as a Platform should: atomically."""
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(state)
    os.replace(temporary, path)


def test_a_missing_file_means_running(tmp_path: Path) -> None:
    assert read_control_file(tmp_path / "state") == "running"


@pytest.mark.parametrize(
    ("text", "state"),
    [("running", "running"), ("paused\n", "paused"), ("  Drain \r\n", "drain"), ("FORCE", "force")],
)
def test_a_state_is_read_whatever_its_case_and_the_whitespace_around_it(tmp_path: Path, text: str, state: str) -> None:
    path = tmp_path / "state"
    path.write_text(text)

    assert read_control_file(path) == state


def test_an_empty_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.write_text(" \n")

    with pytest.raises(ValueError, match=r"state is empty; it should hold running, paused, drain, or force"):
        read_control_file(path)


def test_anything_else_is_an_error_naming_what_the_file_holds(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.write_text("pausd")

    with pytest.raises(ValueError, match=r"state holds 'pausd', not running, paused, drain, or force"):
        read_control_file(path)


def test_long_contents_are_cut_in_the_error(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.write_text("x" * 100)

    with pytest.raises(ValueError, match=r"holds 'x{40}\.\.\.', not"):
        read_control_file(path)


def test_a_file_longer_than_a_kibibyte_is_an_error_whatever_it_starts_with(tmp_path: Path) -> None:
    path = tmp_path / "state"
    path.write_text("force" + " " * 1019)
    assert read_control_file(path) == "force"  # 1024 bytes

    path.write_text("force" + " " * 1024 + "pausd")

    with pytest.raises(ValueError, match=r"state holds more than 1024 bytes; it should hold running, paused"):
        read_control_file(path)


def test_a_file_that_cannot_be_read_raises(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        read_control_file(tmp_path)


async def test_the_source_reads_the_file_anew_each_time(tmp_path: Path) -> None:
    path = tmp_path / "state"
    source = ControlFile(path)

    first = await source.state()
    path.write_text("paused")
    second = await source.state()

    assert (first, second) == ("running", "paused")
    assert repr(source) == f"ControlFile({str(path)!r})"


@pytest.mark.parametrize("state", ["drain", "force"])
async def test_a_stop_in_the_file_an_earlier_process_left_is_told_as_running_until_the_file_is_written_again(
    tmp_path: Path, state: ControlState
) -> None:
    path = tmp_path / "control"
    _write(path, state)
    source = ControlFile(path, left=path.stat())

    told = [await source.state(), await source.state()]
    _write(path, state)  # the same state, written again
    told.append(await source.state())

    assert told == ["running", "running", state]


async def test_a_pause_an_earlier_process_left_is_told(tmp_path: Path) -> None:
    path = tmp_path / "control"
    _write(path, "paused")

    assert await ControlFile(path, left=path.stat()).state() == "paused"


async def test_a_stop_left_is_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="pernix")
    path = tmp_path / "control"
    _write(path, "drain")
    source = ControlFile(path, left=path.stat())

    for _ in range(3):
        await source.state()

    assert [record.getMessage() for record in caplog.records] == [
        f"The control file {path} holds drain, written before this process took the job directory; it is ignored "
        "until the file is written again"
    ]


async def test_once_the_file_left_is_removed_a_stop_written_after_is_told(tmp_path: Path) -> None:
    path = tmp_path / "control"
    _write(path, "drain")
    source = ControlFile(path, left=path.stat())

    path.unlink()
    removed = await source.state()
    _write(path, "drain")

    assert (removed, await source.state()) == ("running", "drain")
