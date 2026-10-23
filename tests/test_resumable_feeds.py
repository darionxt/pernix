"""Feeds of Runs kept in a job directory: written in place, and continued when the Run is resumed."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import orjson
import pytest

from pernix import Pipeline, Request, Spider
from pernix.errors import FeedError, SettingsError
from pernix.testing import FakeFetcher, crawl
from pernix.testing.loopback_server import Reply
from resumable_spider import INDEX, PAGES, Catalog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pernix import ItemContext, Response
    from pernix.testing import LoopbackServer
    from pernix.testing.loopback_server import ReceivedRequest

SITE = "https://shop.example"
SPIDER_FILE = Path(__file__).with_name("resumable_spider.py")


def _site() -> FakeFetcher:
    return FakeFetcher({f"{SITE}/{n}": "<p>page</p>" for n in range(1, 7)})


class Pages(Spider):
    """Follows the pages 1 to 6 one after the other, yielding one Item for each; it may drain at one."""

    name = "pages"
    start_urls = (f"{SITE}/1",)
    drain_at: ClassVar[int] = 0

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        n = int(response.url.rsplit("/", 1)[1])
        yield {"n": n, "text": f"page\n{n}"}
        if n == type(self).drain_at:
            self.crawler.stop()
        if n < 6:
            yield Request(f"{SITE}/{n + 1}")


@pytest.fixture
def pages() -> Iterator[type[Pages]]:
    Pages.drain_at = 0
    yield Pages
    Pages.drain_at = 0


def _records(path: Path) -> list[Any]:
    """The records of a Feed file, parsed as its format says; a CSV file's header comes first."""
    data = path.read_bytes().decode()
    match path.suffix:
        case ".jsonl":
            return [orjson.loads(line) for line in data.splitlines()]
        case ".json":
            return orjson.loads(data)
        case _:
            return list(csv.reader(io.StringIO(data, newline="")))


async def _drained_and_resumed(pages: type[Pages], job: Path, **settings: Any) -> None:
    pages.drain_at = 3
    first = await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), **settings})
    pages.drain_at = 0
    second = await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), **settings})
    assert (first.reason, second.reason) == ("drained", "finished")


@pytest.mark.parametrize("extension", ["jsonl", "json", "csv"])
async def test_a_drained_run_resumed_writes_one_file_holding_the_items_of_both_processes(
    tmp_path: Path, pages: type[Pages], extension: str
) -> None:
    out = tmp_path / f"items.{extension}"

    await _drained_and_resumed(pages, tmp_path / "job", feeds={str(out): {}})

    expected = [{"n": n, "text": f"page\n{n}"} for n in range(1, 7)]
    if extension == "csv":
        assert _records(out) == [["n", "text"], *([str(item["n"]), item["text"]] for item in expected)]
    else:
        assert _records(out) == expected


class Failing(Pipeline):
    def process(self, item: Any, ctx: ItemContext) -> Any:
        raise ValueError("cannot store it")


async def test_the_failed_items_file_is_continued_the_same_way(tmp_path: Path, pages: type[Pages]) -> None:
    failed = tmp_path / "failed.jsonl"

    await _drained_and_resumed(
        pages, tmp_path / "job", pipelines={Failing: 100}, **{"items.failed_output": str(failed)}
    )

    assert [record["item"]["n"] for record in _records(failed)] == [1, 2, 3, 4, 5, 6]


async def test_a_resumed_run_naming_the_same_file_another_way_continues_it(tmp_path: Path, pages: type[Pages]) -> None:
    job, out = tmp_path / "job", tmp_path / "items.jsonl"
    (tmp_path / "sub").mkdir()
    pages.drain_at = 3
    await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {str(out): {}}})
    pages.drain_at = 0

    await crawl(
        pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {str(tmp_path / "sub/../items.jsonl"): {}}}
    )

    assert [record["n"] for record in _records(out)] == [1, 2, 3, 4, 5, 6]


async def test_a_new_run_replaces_an_existing_file(tmp_path: Path, pages: type[Pages]) -> None:
    out = tmp_path / "items.jsonl"
    out.write_text('{"old": true}\n')

    await crawl(pages, fetcher=_site(), settings={"job_dir": str(tmp_path / "job"), "feeds": {str(out): {}}})

    assert [record["n"] for record in _records(out)] == [1, 2, 3, 4, 5, 6]


async def test_a_file_an_earlier_process_never_opened_is_replaced_when_the_run_resumes(
    tmp_path: Path, pages: type[Pages]
) -> None:
    job, out = tmp_path / "job", tmp_path / "items.jsonl"
    out.write_text('{"old": true}\n')
    with pytest.raises(SettingsError, match="both write"):  # the first process fails before it opens its Feeds
        await crawl(
            pages,
            fetcher=_site(),
            settings={"job_dir": str(job), "feeds": {str(out): {}}, "items.failed_output": str(out)},
        )

    report = await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {str(out): {}}})

    assert report.stats["run.resumed"] == 1
    assert [record["n"] for record in _records(out)] == [1, 2, 3, 4, 5, 6]


async def test_a_feed_a_resumed_run_adds_replaces_an_existing_file(tmp_path: Path, pages: type[Pages]) -> None:
    job, kept, added = tmp_path / "job", tmp_path / "kept.jsonl", tmp_path / "added.jsonl"
    pages.drain_at = 3
    await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {str(kept): {}}})
    added.write_text('{"old": true}\n')
    pages.drain_at = 0

    await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {str(kept): {}, str(added): {}}})

    assert [record["n"] for record in _records(kept)] == [1, 2, 3, 4, 5, 6]
    assert [record["n"] for record in _records(added)] == [4, 5, 6]
    assert sorted(json.loads((job / "run.json").read_text())["feeds"]) == [str(added), str(kept)]


async def test_a_file_the_run_wrote_in_another_format_is_refused(tmp_path: Path, pages: type[Pages]) -> None:
    job, out = tmp_path / "job", str(tmp_path / "items.out")
    pages.drain_at = 3
    await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {out: {"format": "jsonl"}}})
    pages.drain_at = 0

    with pytest.raises(FeedError, match=r"cannot continue the feed .+: the Run wrote .+ as jsonl before, not as csv"):
        await crawl(pages, fetcher=_site(), settings={"job_dir": str(job), "feeds": {out: {"format": "csv"}}})


class _HeldPages:
    """Answers the index and the pages of `Catalog`, holding the last two until released."""

    def __init__(self, server: LoopbackServer) -> None:
        self.arrived = threading.Semaphore(0)
        self.release = threading.Event()
        server.route("/index", INDEX, headers=[("Content-Type", "text/html")])
        for n in range(1, PAGES + 1):
            server.route(f"/page/{n}", handler=self._held if n > PAGES - 2 else self._page)

    def _page(self, request: ReceivedRequest) -> Reply:
        return Reply(body=f"<p>{request.path}</p>", headers=[("Content-Type", "text/html")])

    def _held(self, request: ReceivedRequest) -> Reply:
        self.arrived.release()
        self.release.wait(30)
        return self._page(request)

    def wait(self) -> bool:
        """Wait until both held pages arrived; return whether they did."""
        return all(self.arrived.acquire(timeout=30) for _ in range(2))


async def test_a_run_killed_while_writing_and_resumed_leaves_a_file_of_whole_records_with_one_header(
    tmp_path: Path, loopback_server: LoopbackServer
) -> None:
    pages = _HeldPages(loopback_server)
    job, out = tmp_path / "job", tmp_path / "pages.csv"
    feeds = json.dumps({str(out): {}})
    first = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pernix",
        "run",
        f"{SPIDER_FILE}:Catalog",
        "--job-dir",
        str(job),
        "-a",
        f"base={loopback_server.base_url}",
        "-s",
        "network.public_only=false",
        "-s",
        f"feeds={feeds}",
        stderr=asyncio.subprocess.PIPE,
    )
    arrived = await asyncio.to_thread(pages.wait)
    first.kill()  # SIGKILL, while the held pages are in flight
    _, stderr = await first.communicate()
    pages.release.set()
    assert arrived, stderr.decode()

    report = await crawl(
        Catalog, settings={"job_dir": str(job), "feeds": {str(out): {}}}, base=loopback_server.base_url
    )

    [header, *rows] = _records(out)
    assert report.reason == "finished"
    assert header == ["page"]
    assert {row[0] for row in rows} == {loopback_server.url(f"/page/{n}") for n in range(1, PAGES + 1)}
