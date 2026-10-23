"""pernix job-dir: the Run a job directory keeps, its Dead Requests, and reviving them."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from typing import TYPE_CHECKING, Any, ClassVar

import orjson
import pytest

from pernix import Request, Spider
from pernix.cli import main
from pernix.errors import TLSError
from pernix.job_dir import JobDir, RunRecord
from pernix.testing import FakeFetcher, crawl, make_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from pernix import Response
    from pernix.testing import Answer, CrawlReport

SITE = "https://shelf.example"


class Shelf(Spider):
    """Follows the index to four books and an about page; until fixed, the Callback of the second book fails."""

    start_urls = (f"{SITE}/index",)
    settings = {"retry.times": 1}  # noqa: RUF012
    fixed: ClassVar[bool] = False

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        if response.url == f"{SITE}/index":
            for n in range(1, 5):
                yield Request(f"{SITE}/book/{n}", callback=self.parse_book)
            yield Request(f"{SITE}/about")
        else:
            yield {"about": response.url}

    async def parse_book(self, response: Response) -> AsyncIterator[Any]:
        if response.url == f"{SITE}/book/2" and not type(self).fixed:
            raise KeyError("price")
        yield {"book": response.url}


@pytest.fixture
def shelf() -> Iterator[type[Shelf]]:
    Shelf.fixed = False
    yield Shelf
    Shelf.fixed = False


def _unavailable() -> Response:
    return make_response(f"{SITE}/book/3", status=503)


def _site(*book_3: Answer) -> FakeFetcher:
    """The site; its third book is unavailable, unless `book_3` gives its answers in turn."""
    return FakeFetcher(
        {
            f"{SITE}/index": "<p>index</p>",
            f"{SITE}/book/1": "<p>1</p>",
            f"{SITE}/book/2": "<p>2</p>",
            f"{SITE}/book/3": list(book_3) or _unavailable(),
            f"{SITE}/book/4": "<p>4</p>",
            f"{SITE}/about": TLSError("bad certificate"),
        }
    )


def _crawl(spider: type[Spider], job: Path, fetcher: FakeFetcher) -> CrawlReport:
    return asyncio.run(crawl(spider, fetcher=fetcher, settings={"job_dir": str(job), "run_id": "shelf-1"}))


@pytest.fixture
def job(tmp_path: Path, shelf: type[Shelf], capsys: pytest.CaptureFixture[str]) -> Path:
    """A job directory whose Run finished with three Dead Requests: books 2 and 3, and the about page."""
    directory = tmp_path / "job"
    report = _crawl(shelf, directory, _site())
    assert (report.reason, report.stats["requests.failed"]) == ("finished", 3)
    capsys.readouterr()
    return directory


def _job_dir(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = main(["job-dir", *args])
    out, err = capsys.readouterr()
    return code, out, err


def _contents(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


def test_status_tells_the_run_and_how_many_of_its_requests_are_in_each_state(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    record = json.loads((job / "run.json").read_text())
    started = dt.datetime.fromisoformat(record["started_at"]).isoformat(timespec="milliseconds")

    code, out, _ = _job_dir(capsys, "status", str(job))

    assert code == 0
    assert out.splitlines() == [
        "Run: shelf-1",
        "Spider: shelf",
        f"First started: {started}",
        f"Started by: Pernix {record['pernix']}",
        "start(): iterated to its end",
        "Requests: 0 ready, 0 delayed, 0 leased, 3 done, 3 dead",
    ]
    code, out, _ = _job_dir(capsys, "status", str(job), "--json")
    assert code == 0
    assert orjson.loads(out) == {
        "schema": 1,
        "run_id": "shelf-1",
        "spider": "shelf",
        "started_at": started,
        "start_exhausted": True,
        "pernix": record["pernix"],
        "requests": {"ready": 0, "delayed": 0, "leased": 0, "done": 3, "dead": 3},
    }


def test_the_dead_requests_are_listed_oldest_first_with_their_reasons(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _job_dir(capsys, "dead", str(job))

    assert code == 0
    assert [line.split("\t")[1:] for line in out.splitlines()] == [
        ["1", "parse_book", f"{SITE}/book/2", "KeyError: 'price'"],
        ["2", "parse_book", f"{SITE}/book/3", "RetriesExhausted: HTTP 503"],
        ["1", "parse", f"{SITE}/about", "TLSError: bad certificate"],
    ]
    code, out, _ = _job_dir(capsys, "dead", str(job), "--json")
    assert code == 0
    listed = [orjson.loads(line) for line in out.splitlines()]
    assert [line.split("\t")[0] for line in _job_dir(capsys, "dead", str(job))[1].splitlines()] == [
        dead["id"] for dead in listed
    ]
    assert listed[1] == {
        "schema": 1,
        "id": listed[1]["id"],
        "url": f"{SITE}/book/3",
        "error": "RetriesExhausted",
        "reason": "RetriesExhausted: HTTP 503",
        "attempts": 2,
        "spent": {},
        "callback": "parse_book",
        "errback": None,
        "rendered": False,
    }
    assert (listed[2]["error"], listed[2]["callback"]) == ("TLSError", None)


@pytest.mark.parametrize(
    ("filters", "urls"),
    [
        (["--error", "KeyError"], ["/book/2"]),
        (["--error", "KeyError", "--error", "TLSError"], ["/book/2", "/about"]),
        (["--reason", "HTTP 503"], ["/book/3"]),
        (["--callback", "parse_book"], ["/book/2", "/book/3"]),
        (["--callback", "parse"], ["/about"]),
        (["--callback", "parse_book", "--reason", "503"], ["/book/3"]),
        (["--error", "Key"], []),
    ],
)
def test_filters_pick_any_of_their_values_and_must_all_match(
    job: Path, capsys: pytest.CaptureFixture[str], filters: list[str], urls: list[str]
) -> None:
    code, out, _ = _job_dir(capsys, "dead", str(job), *filters)

    assert code == 0
    assert [line.split("\t")[3] for line in out.splitlines()] == [f"{SITE}{url}" for url in urls]


def test_revived_requests_are_fetched_by_the_next_resumed_run_their_attempts_counted_from_1(
    job: Path, shelf: type[Shelf], capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _job_dir(capsys, "revive", str(job), "--callback", "parse_book")

    assert code == 0
    assert err == f"Revived Dead Requests in {job}: 2 of 3; the next Run resumed from it fetches them.\n"
    assert "Requests: 2 ready, 0 delayed, 0 leased, 3 done, 1 dead" in _job_dir(capsys, "status", str(job))[1]
    shelf.fixed = True
    site = _site(_unavailable(), "<p>3</p>")  # once more unavailable, which a retry outlasts

    report = _crawl(shelf, job, site)

    assert [request.url for request in site.requests] == [f"{SITE}/book/2", f"{SITE}/book/3", f"{SITE}/book/3"]
    assert sorted(item["book"] for item in report.items) == [f"{SITE}/book/2", f"{SITE}/book/3"]
    assert [line.split("\t")[3] for line in _job_dir(capsys, "dead", str(job))[1].splitlines()] == [f"{SITE}/about"]


def test_reviving_needs_filters_or_all(job: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before = _contents(job)

    code, _, err = _job_dir(capsys, "revive", str(job))

    assert code == 2
    assert "give filters (--error, --reason, or --callback) for the Dead Requests to revive, or --all" in err
    assert _job_dir(capsys, "revive", str(job), "--all", "--error", "KeyError")[0] == 2
    assert _contents(job) == before
    code, _, err = _job_dir(capsys, "revive", str(job), "--all")
    assert (code, err) == (0, f"Revived Dead Requests in {job}: 3 of 3; the next Run resumed from it fetches them.\n")
    assert _job_dir(capsys, "revive", str(job), "--all")[2] == f"Revived Dead Requests in {job}: 0 of 0.\n"


def test_a_job_directory_a_run_holds_is_refused_with_exit_code_2_and_left_as_it_was(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    holder = JobDir(job)
    recorded = holder.peek()
    assert recorded is not None
    holder.take(recorded)  # as the Run resumed from it does
    before = _contents(job)
    try:
        for command in (["status"], ["dead"], ["revive", "--all"]):
            code, out, err = _job_dir(capsys, *command, str(job))

            assert (code, out) == (2, "")
            assert f"Error: the job directory {job} is used by process " in err
    finally:
        holder.release()

    assert _contents(job) == before


def test_a_directory_that_is_not_a_job_directory_is_refused_and_left_as_it_was(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "notes.txt").write_text("not a Run")

    code, _, err = _job_dir(capsys, "status", str(tmp_path))

    assert code == 2
    assert f"Error: {tmp_path} is not a job directory: it holds no record of a Run in run.json" in err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["notes.txt"]
    code, _, err = _job_dir(capsys, "dead", str(tmp_path / "missing"))
    assert (code, err) == (2, f"Error: there is no job directory {tmp_path / 'missing'}\n")
    assert not (tmp_path / "missing").exists()


def test_a_run_recorded_without_a_frontier_has_no_requests_and_gets_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = JobDir(tmp_path)
    job.take(RunRecord(run_id="r1", spider="shelf", started_at=dt.datetime.now(dt.UTC), pernix="1.0"))
    job.release()

    assert "Requests: 0 ready, 0 delayed, 0 leased, 0 done, 0 dead" in _job_dir(capsys, "status", str(tmp_path))[1]
    assert _job_dir(capsys, "dead", str(tmp_path))[:2] == (0, "")
    assert not (tmp_path / "frontier.db").exists()


def test_a_frontier_a_newer_pernix_wrote_is_refused_with_exit_code_2(
    job: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with closing(sqlite3.connect(job / "frontier.db", autocommit=True)) as database:
        database.execute("PRAGMA user_version = 99")

    code, _, err = _job_dir(capsys, "dead", str(job))

    assert code == 2
    assert "has schema version 99, but this Pernix reads version" in err


def test_reasons_are_listed_on_one_line_each(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class Split(Spider):
        name = "split"
        start_urls = (f"{SITE}/index",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            raise ValueError("first line\nsecond\tline")
            yield

    _crawl(Split, tmp_path, FakeFetcher({f"{SITE}/index": "<p>index</p>"}))
    capsys.readouterr()

    out = _job_dir(capsys, "dead", str(tmp_path))[1]
    assert out.split("\t")[1:] == ["1", "parse", f"{SITE}/index", "ValueError: first line second line\n"]
    assert orjson.loads(_job_dir(capsys, "dead", str(tmp_path), "--json")[1])["reason"] == (
        "ValueError: first line\nsecond\tline"
    )


class _ClosedStdout:
    """A stdout whose reader went away."""

    def write(self, text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def isatty(self) -> bool:
        return False


def test_a_listing_whose_stdout_was_closed_exits_with_1(job: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdout", _ClosedStdout())

    assert main(["job-dir", "dead", str(job)]) == 1


def test_the_command_runs_in_a_process_of_its_own(job: Path) -> None:
    completed = subprocess.run(  # noqa: S603  # this interpreter, running the command under test
        [sys.executable, "-m", "pernix", "job-dir", "dead", str(job), "--error", "TLSError"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert (completed.returncode, completed.stdout.split("\t")[3]) == (0, f"{SITE}/about")
