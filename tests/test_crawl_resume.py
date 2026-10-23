"""Runs kept in a job directory: stopped, killed, and resumed."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import sqlite3
import sys
import threading
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Request, Spider
from pernix.errors import JobDirError, SettingsError
from pernix.frontiers import SQLiteFrontier
from pernix.job_dir import JobDir, RunRecord
from pernix.testing import FakeFetcher, crawl
from pernix.testing.loopback_server import Reply
from resumable_spider import INDEX, PAGES, Catalog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pernix import Response
    from pernix.testing import LoopbackServer
    from pernix.testing.loopback_server import ReceivedRequest

SITE = "https://shop.example"
SPIDER_FILE = Path(__file__).with_name("resumable_spider.py")


def _site(*pages: int) -> FakeFetcher:
    return FakeFetcher({f"{SITE}/{n}": "<p>page</p>" for n in pages})


class Chain(Spider):
    """Follows the pages 1 to 6 one after the other; a test may have it drain at one of them."""

    name = "chain"
    start_urls = (f"{SITE}/1",)
    drain_at: ClassVar[int] = 0
    starts: ClassVar[int] = 0

    async def start(self) -> AsyncIterator[Request]:
        type(self).starts += 1
        async for request in super().start():
            yield request

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        n = int(response.url.rsplit("/", 1)[1])
        yield {"n": n}
        if n == type(self).drain_at:
            self.crawler.stop()
        if n < 6:
            yield Request(f"{SITE}/{n + 1}", callback=self.parse)


@pytest.fixture
def chain() -> Iterator[type[Chain]]:
    Chain.drain_at, Chain.starts = 0, 0
    yield Chain
    Chain.drain_at, Chain.starts = 0, 0


def _settings(job: Path, **more: Any) -> dict[str, Any]:
    return {"job_dir": str(job), **more}


def _record() -> RunRecord:
    """A Run of `Chain`, as another process holding its job directory recorded it."""
    return RunRecord(run_id="held", spider="chain", started_at=dt.datetime.now(dt.UTC))


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _first_start(job: Path) -> str:
    """When the Run of a job directory first started, as the stats and the outcome file tell times."""
    recorded = json.loads((job / "run.json").read_text())["started_at"]
    return dt.datetime.fromisoformat(recorded).isoformat(timespec="milliseconds")


async def test_a_drained_run_run_again_continues_where_it_stopped_and_finishes(
    tmp_path: Path, chain: type[Chain]
) -> None:
    chain.drain_at = 3
    first = await crawl(chain, fetcher=_site(1, 2, 3), settings=_settings(tmp_path))
    chain.drain_at = 0
    site = _site(4, 5, 6)

    second = await crawl(chain, fetcher=site, settings=_settings(tmp_path))

    assert (first.reason, [item["n"] for item in first.items]) == ("drained", [1, 2, 3])
    assert (second.reason, [item["n"] for item in second.items]) == ("finished", [4, 5, 6])
    assert [request.url for request in site.requests] == [f"{SITE}/{n}" for n in (4, 5, 6)]
    assert second.run_id == first.run_id
    assert (second.stats["run.resumed"], second.stats["frontier.done"]) == (1, 6)
    assert second.stats["run.first_started_at"] == _first_start(tmp_path)
    assert "run.resumed" not in first.stats


@pytest.mark.parametrize("empty", ["", "."])
async def test_an_empty_job_dir_keeps_the_frontier_in_memory_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chain: type[Chain], empty: str
) -> None:
    monkeypatch.chdir(tmp_path)

    report = await crawl(chain, fetcher=_site(1, 2, 3, 4, 5, 6), settings={"job_dir": empty})

    assert (report.reason, len(report.items)) == ("finished", 6)
    assert not any(key.startswith(("run.resumed", "control.")) for key in report.stats)
    assert _names(tmp_path) == []


async def test_start_iterated_to_its_end_is_not_iterated_again(tmp_path: Path, chain: type[Chain]) -> None:
    chain.drain_at = 1
    await crawl(chain, fetcher=_site(1), settings=_settings(tmp_path))

    report = await crawl(chain, fetcher=_site(2, 3, 4, 5, 6), settings=_settings(tmp_path))

    assert chain.starts == 1
    assert report.stats.get("requests.dropped_by_reason.duplicate") is None


class Seeds(Spider):
    """Yields three pages from `start()`, draining the Run after the second the first time."""

    name = "seeds"
    drained: ClassVar[bool] = False

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{SITE}/1")
        yield Request(f"{SITE}/2", dont_filter=True)
        if not type(self).drained:
            type(self).drained = True
            self.crawler.stop()
        yield Request(f"{SITE}/3")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"url": response.url}


async def test_start_stopped_before_its_end_is_iterated_again_its_requests_added_before_being_duplicates(
    tmp_path: Path,
) -> None:
    Seeds.drained = False
    first = await crawl(Seeds, fetcher=_site(1, 2, 3), settings=_settings(tmp_path))
    site = _site(1, 2, 3)

    second = await crawl(Seeds, fetcher=site, settings=_settings(tmp_path))

    assert sorted(item["url"] for item in first.items) == [f"{SITE}/1", f"{SITE}/2"]
    assert [request.url for request in site.requests] == [f"{SITE}/2", f"{SITE}/3"]  # dont_filter adds it again
    assert second.stats["requests.dropped_by_reason.duplicate"] == 1


class Other(Chain):
    name = "other"


class Keyword(Spider):
    name = "keyword"

    def __init__(self, keyword: str) -> None:
        self.keyword = keyword

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{SITE}/1")
        self.crawler.stop()

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"keyword": self.keyword}


@pytest.mark.parametrize(
    ("first", "then", "difference"),
    [
        ((Chain, {}, {}), (Other, {}, {}), "the Spider is 'other', not 'chain'"),
        ((Chain, {}, {"run_id": "a"}), (Chain, {}, {"run_id": "b"}), "run_id is 'b', not 'a'"),
        ((Keyword, {"keyword": "dune"}, {}), (Keyword, {"keyword": "emma"}, {}), "keyword is 'emma', not 'dune'"),
        (
            (Chain, {}, {}),
            (Chain, {}, {"fingerprint.headers": ["X-Lang"]}),
            "fingerprint.headers is ['X-Lang'], not []",
        ),
    ],
)
async def test_another_run_on_the_directory_is_a_configuration_error_and_fetches_nothing(
    tmp_path: Path,
    chain: type[Chain],
    first: tuple[type[Spider], dict[str, Any], dict[str, Any]],
    then: tuple[type[Spider], dict[str, Any], dict[str, Any]],
    difference: str,
) -> None:
    chain.drain_at = 1
    spider, args, settings = first
    await crawl(spider, fetcher=_site(1, 2), settings=_settings(tmp_path, **settings), **args)
    before = (tmp_path / "run.json").read_bytes()
    spider, args, settings = then
    site = _site(1, 2)

    with pytest.raises(JobDirError, match=r"which this Run does not continue:\n") as raised:
        await crawl(spider, fetcher=site, settings=_settings(tmp_path, **settings), **args)

    assert difference in str(raised.value)
    assert site.requests == []
    assert (tmp_path / "run.json").read_bytes() == before


def by_url(request: Request) -> str:
    return request.url


class ByUrl:
    def __call__(self, request: Request) -> str:
        return request.url


@pytest.mark.parametrize("function", [by_url, ByUrl()], ids=["function", "callable object"])
async def test_a_fingerprint_function_given_as_an_object_is_recorded_by_its_name(
    tmp_path: Path, chain: type[Chain], function: object
) -> None:
    chain.drain_at = 1
    await crawl(chain, fetcher=_site(1), settings=_settings(tmp_path, **{"fingerprint.function": function}))
    chain.drain_at = 0

    report = await crawl(
        chain, fetcher=_site(2, 3, 4, 5, 6), settings=_settings(tmp_path, **{"fingerprint.function": function})
    )

    assert report.reason == "finished"
    recorded = json.loads((tmp_path / "run.json").read_text())["fingerprint"]["function"]
    assert recorded in {"test_crawl_resume.by_url", "test_crawl_resume.ByUrl"}


class Pages(Spider):
    name = "pages"

    def __init__(self, pages: tuple[int, ...], labels: dict[int, str]) -> None:
        self.pages = pages

    async def start(self) -> AsyncIterator[Request]:
        for n in self.pages:
            yield Request(f"{SITE}/{n}")
        self.crawler.stop()

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"url": response.url}


async def test_spider_arguments_compare_as_the_job_directory_records_them(tmp_path: Path) -> None:
    await crawl(Pages, fetcher=_site(1, 2), settings=_settings(tmp_path), pages=(1, 2), labels={1: "one"})

    report = await crawl(Pages, fetcher=_site(1, 2), settings=_settings(tmp_path), pages=(1, 2), labels={1: "one"})

    assert report.stats["run.resumed"] == 1


@pytest.mark.parametrize("name", ["run.json", "frontier.db", "lock", "control"])
async def test_no_output_may_write_a_file_the_job_directory_keeps(
    tmp_path: Path, chain: type[Chain], name: str
) -> None:
    chain.drain_at = 1
    await crawl(chain, fetcher=_site(1), settings=_settings(tmp_path))
    record = (tmp_path / "run.json").read_bytes()

    with pytest.raises(SettingsError, match=rf"job_dir \({name}\) and outcome_file both write "):
        await crawl(chain, fetcher=_site(2), settings=_settings(tmp_path, outcome_file=str(tmp_path / name)))
    with pytest.raises(SettingsError, match=rf"job_dir \({name}\) both write "):
        await crawl(
            chain, fetcher=_site(2), settings=_settings(tmp_path, feeds={str(tmp_path / name): {"format": "jsonl"}})
        )

    assert (tmp_path / "run.json").read_bytes() == record


class Renamed(Chain):
    """`Chain` once its Callback `parse_next` was renamed."""


class Named(Chain):
    async def parse(self, response: Response) -> AsyncIterator[Any]:
        self.crawler.stop()  # first, so that no worker claims the Request yielded
        yield Request(f"{SITE}/2", callback=self.parse_next)

    async def parse_next(self, response: Response) -> AsyncIterator[Any]:
        yield {"url": response.url}


async def test_requests_naming_methods_the_spider_no_longer_has_stop_the_run_before_it_fetches(
    tmp_path: Path,
) -> None:
    await crawl(Named, fetcher=_site(1), settings=_settings(tmp_path))
    site = _site(2)

    with pytest.raises(JobDirError, match=r"name methods the Spider 'chain' does not have: parse_next; were they"):
        await crawl(Renamed, fetcher=site, settings=_settings(tmp_path))

    assert site.requests == []


class Unstorable(Spider):
    name = "unstorable"
    start_urls = (f"{SITE}/1",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        if response.url == f"{SITE}/1":
            yield Request(f"{SITE}/2", meta={"when": dt.datetime.now(dt.UTC)})
            yield Request(f"{SITE}/3")


async def test_a_request_the_frontier_cannot_store_is_dropped_and_the_run_goes_on(tmp_path: Path) -> None:
    site = _site(1, 2, 3)

    report = await crawl(Unstorable, fetcher=site, settings=_settings(tmp_path))

    assert report.reason == "finished"
    assert [request.url for request in site.requests] == [f"{SITE}/1", f"{SITE}/3"]
    assert (report.stats["requests.dropped_by_reason.unpersistable"], report.stats["log.error"]) == (1, 1)


async def test_requests_the_last_process_left_leased_are_fetched_again_at_once(
    tmp_path: Path, chain: type[Chain]
) -> None:
    chain.drain_at = 2
    await crawl(chain, fetcher=_site(1, 2), settings=_settings(tmp_path))
    frontier = SQLiteFrontier(tmp_path / "frontier.db", chain())
    [lease] = await frontier.claim("killed-worker", 1, 3600.0)  # as a process killed while fetching it
    await frontier.close()
    site = _site(3, 4, 5, 6)

    async with asyncio.timeout(30):  # rather than once the Lease expires, in an hour
        report = await crawl(chain, fetcher=site, settings=_settings(tmp_path, **{"frontier.lease_ttl": 3600}))

    assert lease.request.url == f"{SITE}/3"
    assert (report.reason, site.requests[0].url) == ("finished", f"{SITE}/3")


async def test_a_refused_run_writes_no_outcome_file_in_the_job_directory_but_does_elsewhere(
    tmp_path: Path, chain: type[Chain]
) -> None:
    job = tmp_path / "job"
    holder = JobDir(job)
    holder.take(_record())
    try:
        for outcome in (job / "outcome.json", tmp_path / "outcome.json"):
            with pytest.raises(JobDirError, match=r"is used by process "):
                await crawl(chain, fetcher=_site(1), settings=_settings(job, outcome_file=str(outcome)))
    finally:
        holder.release()

    assert not (job / "outcome.json").exists()
    assert json.loads((tmp_path / "outcome.json").read_text())["exit_code"] == 2


async def test_a_resumed_run_tells_its_first_start_to_its_outcome_file(tmp_path: Path, chain: type[Chain]) -> None:
    chain.drain_at = 1
    await crawl(chain, fetcher=_site(1), settings=_settings(tmp_path / "job"))

    await crawl(
        chain,
        fetcher=_site(2, 3, 4, 5, 6),
        settings=_settings(tmp_path / "job", outcome_file=str(tmp_path / "outcome.json")),
    )

    outcome = json.loads((tmp_path / "outcome.json").read_text())
    assert outcome["started_at"] == _first_start(tmp_path / "job")


# Processes of their own


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


def _pernix_run(job: Path, server: LoopbackServer, *more: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pernix",
        "run",
        f"{SPIDER_FILE}:Catalog",
        "--job-dir",
        str(job),
        "-a",
        f"base={server.base_url}",
        "-s",
        "network.public_only=false",
        *more,
    ]


async def test_a_run_killed_mid_crawl_fetches_again_no_request_done_and_each_one_in_flight_at_most_once(
    tmp_path: Path, loopback_server: LoopbackServer
) -> None:
    pages = _HeldPages(loopback_server)
    job = tmp_path / "job"
    first = await asyncio.create_subprocess_exec(*_pernix_run(job, loopback_server), stderr=asyncio.subprocess.PIPE)
    arrived = await asyncio.to_thread(pages.wait)
    first.kill()  # SIGKILL, while the held pages are in flight
    _, stderr = await first.communicate()
    pages.release.set()
    assert arrived, stderr.decode()
    with closing(sqlite3.connect(job / "frontier.db")) as database:
        leased = {url for (url,) in database.execute("SELECT url FROM requests WHERE state = 'leased'")}
        kept = {url for (url,) in database.execute("SELECT url FROM requests")}
    before = [request.path for request in loopback_server.received]

    report = await crawl(Catalog, settings={"job_dir": str(job)}, base=loopback_server.base_url)

    again = [request.path for request in loopback_server.received][len(before) :]
    pages_kept = {url.removeprefix(loopback_server.base_url) for url in kept}
    in_flight = {url.removeprefix(loopback_server.base_url) for url in leased}
    assert report.reason == "finished"
    assert {f"/page/{n}" for n in (PAGES - 1, PAGES)} <= in_flight
    assert sorted(again) == sorted(pages_kept)  # each Request not done, once
    assert {path for path in [*before, *again] if path.startswith("/page/")} == {
        f"/page/{n}" for n in range(1, PAGES + 1)
    }
    assert report.stats["run.resumed"] == 1


async def test_a_second_process_on_the_directory_fails_with_exit_code_2_and_changes_nothing(
    tmp_path: Path, loopback_server: LoopbackServer
) -> None:
    job = tmp_path / "job"
    holder = JobDir(job)
    holder.take(_record())
    before = {path.name: path.read_bytes() for path in job.iterdir()}
    try:
        second = await asyncio.create_subprocess_exec(
            *_pernix_run(job, loopback_server, "-s", f"outcome_file={job / 'outcome.json'}"),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await second.communicate()
    finally:
        holder.release()

    assert second.returncode == 2
    assert b"is used by process" in stderr
    assert {path.name: path.read_bytes() for path in job.iterdir()} == before
    assert loopback_server.received == []
