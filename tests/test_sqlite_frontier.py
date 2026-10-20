from __future__ import annotations

import asyncio
import datetime as dt
import gc
import os
import random
import sqlite3
import stat
import sys
import time
import weakref
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Response, Spider, events
from pernix.callbacks import run_callbacks
from pernix.engine import Engine
from pernix.errors import SerializationError
from pernix.frontiers import FrontierCounts, Lease, MemoryFrontier, SQLiteFrontier
from pernix.frontiers.sqlite_store import SCHEMA_VERSION
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import RetrySettings
from pernix.testing import FakeFetcher, make_response
from pernix.testing.frontier import FakeClock, FrontierConformance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from pernix.frontiers import Frontier
    from pernix.items import ItemContext

URL = "https://example.com/"


class Shop(Spider):
    async def parse_item(self, response: Response) -> None:
        pass


class Renamed(Spider):
    """The Spider `Shop` once its Callback `parse_item` was renamed."""

    name = "shop"

    async def parse_product(self, response: Response) -> None:
        pass


class TestSQLiteFrontier(FrontierConformance):
    @pytest.fixture(autouse=True)
    def _storage(self, tmp_path: Path) -> None:
        self.path = tmp_path / "frontier.db"

    def make_frontier(self, clock: Callable[[], float]) -> Frontier:
        return SQLiteFrontier(self.path, Shop(), clock=clock)

    async def reopen(self, frontier: Frontier, clock: Callable[[], float]) -> Frontier:
        return SQLiteFrontier(self.path, Shop(), clock=clock)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "frontier.db"


@pytest.fixture
async def opened(path: Path) -> AsyncIterator[Callable[..., SQLiteFrontier]]:
    """Opens SQLiteFrontiers on `path`, each closed after the test."""
    frontiers: list[SQLiteFrontier] = []

    def open_frontier(spider: Spider | None = None, *, clock: Callable[[], float] = time.time) -> SQLiteFrontier:
        frontiers.append(SQLiteFrontier(path, spider or Shop(), clock=clock))
        return frontiers[-1]

    yield open_frontier
    for frontier in frontiers:
        await frontier.close()


type Opener = Callable[..., SQLiteFrontier]


def _database(path: Path) -> closing[sqlite3.Connection]:
    return closing(sqlite3.connect(path, autocommit=True))


async def test_callbacks_come_back_as_methods_of_the_spider_of_a_later_process(opened: Opener) -> None:
    first, later = Shop(), Shop()
    frontier = opened(first)
    await frontier.add([Request(URL, callback=first.parse_item, meta={"depth": 1})])
    await frontier.close()

    [lease] = await opened(later).claim("worker-1", 1, 30.0)

    assert lease.request.callback == later.parse_item
    assert lease.request.meta == {"depth": 1}


async def test_a_request_that_cannot_be_stored_adds_nothing_of_the_batch(opened: Opener) -> None:
    frontier = opened()

    with pytest.raises(SerializationError, match=r"meta\['when'\] holds a datetime"):
        await frontier.add([Request(URL + "good"), Request(URL + "bad", meta={"when": dt.datetime.now(dt.UTC)})])

    assert (await frontier.counts()).ready == 0
    assert await frontier.add([Request(URL + "good")]) == [True]


async def test_a_request_that_cannot_be_restored_fails_the_claim_and_stays_ready(opened: Opener) -> None:
    shop = Shop()
    frontier = opened(shop)
    await frontier.add([Request(URL, callback=shop.parse_item)])
    await frontier.close()
    frontier = opened(Renamed())

    with pytest.raises(
        SerializationError, match=r"the Request 1 for https://example\.com/ cannot be restored: .*'parse_item'"
    ):
        await frontier.claim("worker-1", 1, 30.0)

    counts = await frontier.counts()
    assert (counts.ready, counts.leased) == (1, 0)


async def test_done_requests_keep_only_their_fingerprint(opened: Opener, path: Path) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert await frontier.ack(lease)
    await frontier.close()

    with _database(path) as database:
        assert database.execute("SELECT count(*) FROM requests").fetchall() == [(0,)]
        assert database.execute("SELECT count(*) FROM fingerprints").fetchall() == [(1,)]


async def test_dead_requests_keep_their_request_and_reason(opened: Opener, path: Path) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert await frontier.dead(lease, "ValueError: cannot parse")
    await frontier.close()

    with _database(path) as database:
        rows = database.execute("SELECT state, url, reason, worker FROM requests").fetchall()

    assert rows == [("dead", URL, "ValueError: cannot parse", "worker-1")]


async def test_a_database_of_a_newer_schema_is_refused_naming_both_versions(opened: Opener, path: Path) -> None:
    frontier = opened()
    await frontier.counts()
    await frontier.close()
    with _database(path) as database:
        database.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(SerializationError, match=r"has schema version 2, but this Pernix reads version 1 and older"):
        await opened().counts()


def _another_database(path: Path) -> None:
    with _database(path) as database:
        database.execute("CREATE TABLE notes (text TEXT)")


def _not_a_database(path: Path) -> None:
    path.write_bytes(b"neither SQLite nor anything else" * 100)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (_another_database, r"is not a Frontier database$"),
        (_not_a_database, r"is not a Frontier database: file is not a database"),
    ],
)
async def test_a_file_that_is_not_a_frontiers_database_is_refused(
    opened: Opener, path: Path, make: Callable[[Path], None], message: str
) -> None:
    make(path)

    with pytest.raises(SerializationError, match=message):
        await opened().add([Request(URL)])


@pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX")
async def test_a_new_database_is_readable_by_its_owner_only(opened: Opener, path: Path) -> None:
    await opened().add([Request(URL)])

    modes = {name: stat.S_IMODE(os.stat(f"{path}{name}").st_mode) for name in ("", "-wal", "-shm")}

    assert modes == {"": 0o600, "-wal": 0o600, "-shm": 0o600}


async def test_operations_wait_for_the_database_off_the_event_loop(opened: Opener, path: Path) -> None:
    frontier = opened()
    await frontier.counts()
    with _database(path) as holder:
        holder.execute("BEGIN IMMEDIATE")  # holds the database, as a slow disk would
        adding = asyncio.create_task(frontier.add([Request(URL)]))
        for _ in range(10):
            await asyncio.sleep(0)  # the event loop goes on meanwhile
        assert not adding.done()
        holder.execute("COMMIT")

    assert await adding == [True]


async def test_a_claim_cancelled_while_it_waits_gives_its_leases_back(opened: Opener, path: Path) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])
    with _database(path) as holder:
        holder.execute("BEGIN IMMEDIATE")
        claiming = asyncio.create_task(frontier.claim("worker-1", 1, 30.0))
        await asyncio.sleep(0)
        claiming.cancel()
        holder.execute("COMMIT")

    with pytest.raises(asyncio.CancelledError):
        await claiming
    counts = await frontier.counts()
    assert (counts.ready, counts.leased) == (1, 0)
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert lease.attempt == 1


async def test_wait_returns_for_a_change_made_since_the_task_last_used_the_frontier(opened: Opener) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    assert (await frontier.counts()).leased == 1  # as the Engine finds a Run not idle yet
    await asyncio.create_task(frontier.ack(lease))  # settled by another task, before this one waits

    async with asyncio.timeout(5):
        await frontier.wait()


async def test_wait_returns_at_once_for_a_change_only_once(opened: Opener) -> None:
    frontier = opened()
    await frontier.counts()
    await asyncio.create_task(frontier.add([Request(URL)]))
    await frontier.wait()  # for the Request added since this task looked

    waiting = asyncio.create_task(frontier.wait())
    for _ in range(10):
        await asyncio.sleep(0)
    assert not waiting.done()

    await frontier.add([Request(URL + "next")])
    async with asyncio.timeout(5):
        await waiting


async def test_a_task_that_used_a_frontier_does_not_keep_it(path: Path) -> None:
    frontier = SQLiteFrontier(path, Shop())
    await frontier.counts()  # in this task, whose context lives on
    await frontier.close()
    used = weakref.ref(frontier)

    del frontier
    gc.collect()

    assert used() is None


async def test_an_operation_takes_the_time_once_it_holds_the_database(opened: Opener, path: Path) -> None:
    clock = FakeClock()
    frontier = opened(clock=clock)
    await frontier.add([Request(URL)])
    with _database(path) as holder:
        holder.execute("BEGIN IMMEDIATE")
        claiming = asyncio.create_task(frontier.claim("worker-1", 1, 10.0))
        await asyncio.sleep(0)
        clock.advance(20.0)  # while the claim waits for the database
        holder.execute("COMMIT")

    [lease] = await claiming
    assert lease.expires_at == clock.now + 10.0


async def test_wait_returns_when_a_delay_passes(opened: Opener) -> None:
    clock = FakeClock()
    frontier = opened(clock=clock)
    await frontier.add([Request(URL)])
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    await frontier.nack(lease, delay=0.05)
    waiting = asyncio.create_task(frontier.wait())
    clock.advance(0.05)

    async with asyncio.timeout(5):
        await waiting

    assert (await frontier.counts()).ready == 1


async def test_deadlines_are_on_the_wall_clock_by_default(opened: Opener) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])

    before = time.time()
    [lease] = await frontier.claim("worker-1", 1, 30.0)
    after = time.time()

    assert before + 30.0 <= lease.expires_at <= after + 30.0


async def test_closing_again_does_nothing_and_later_operations_are_refused(opened: Opener) -> None:
    frontier = opened()
    await frontier.add([Request(URL)])

    await frontier.close()
    await frontier.close()

    with pytest.raises(RuntimeError, match="the Frontier is closed"):
        await frontier.counts()


class Pages(Spider):
    start_urls = ("https://site.example/1",)

    async def parse(self, response: Response) -> AsyncIterator[Request]:
        for n in range(2, 6):
            yield Request(f"https://site.example/{n}", callback=self.parse_page, kwargs={"n": n})

    async def parse_page(self, response: Response, n: int) -> AsyncIterator[dict[str, int]]:
        yield {"n": n}


def _unavailable(request: Request) -> Response:
    return make_response(request.url, status=503, request=request)


async def test_an_engine_crawls_through_it(opened: Opener) -> None:
    spider = Pages()
    site = FakeFetcher({f"https://site.example/{n}": "<p>page</p>" for n in (1, 2, 4, 5)})
    site.add("https://site.example/3", [_unavailable, "<p>page</p>"])  # retried once
    frontier = opened(spider)
    items: list[Any] = []

    async def put_item(item: Any, _ctx: ItemContext) -> None:
        items.append(item)

    engine = Engine(
        spider,
        frontier=frontier,
        download=site.fetch,
        handle=run_callbacks(spider),
        put_item=put_item,
        events=events.EventBus(timeout=5),
        concurrency=4,
        retry_policy=DefaultRetryPolicy(RetrySettings(delay_scale=0)),
    )
    async with asyncio.timeout(10):
        await engine.run()

    assert sorted(item["n"] for item in items) == [2, 3, 4, 5]
    assert len(site.requests) == 6
    assert await frontier.counts() == FrontierCounts(ready=0, delayed=0, leased=0, done=5, dead=0)


def _described(lease: Lease) -> tuple[Any, ...]:
    return (
        lease.id,
        lease.token,
        lease.attempt,
        lease.expires_at,
        lease.slot,
        lease.request,
        lease.rendered,
        dict(lease.spent),
    )


async def test_it_hands_out_requests_as_the_memory_frontier_does(opened: Opener) -> None:
    rng = random.Random(106)  # noqa: S311  # seeded, the same steps every run
    clock = FakeClock()
    memory, frontier = MemoryFrontier(clock=clock), opened(clock=clock)
    hosts = ["a.example", "b.example", "c.example"]
    leases: list[tuple[Lease, Lease]] = []  # stale ones included
    for _ in range(1500):
        action = rng.choice(["add", "claim", "settle", "renew", "advance"])
        if action == "add":
            requests = [
                Request(
                    f"https://{rng.choice(hosts)}/{rng.randrange(200)}",
                    priority=rng.choice([0, 0, 1, 5]),
                    render=rng.random() < 0.3,
                    dont_filter=rng.random() < 0.05,
                )
                for _ in range(rng.randint(1, 4))
            ]
            assert await frontier.add(requests) == await memory.add(requests)
        elif action == "claim":
            limit, ttl = rng.randint(1, 3), rng.choice([5.0, 30.0])
            skip, rendered = set(rng.sample(hosts, rng.randint(0, 2))), rng.choice([None, True, False])
            expected = await memory.claim("worker-1", limit, ttl, skip, rendered=rendered)
            claimed = await frontier.claim("worker-1", limit, ttl, skip, rendered=rendered)
            assert list(map(_described, claimed)) == list(map(_described, expected))
            leases.extend(zip(expected, claimed, strict=True))
        elif action == "settle" and leases:
            expected, lease = leases[rng.randrange(len(leases))]
            match rng.choice(["ack", "nack", "dead"]):
                case "ack":
                    assert await frontier.ack(lease) == await memory.ack(expected)
                case "nack":
                    options: dict[str, Any] = {
                        "delay": rng.choice([0.0, 0.0, 3.0, 10.0]),
                        "count_attempt": rng.random() < 0.7,
                        "budget": rng.choice([None, None, "blocked"]),
                    }
                    if rng.random() < 0.2:
                        options["request"] = lease.request.replace(render=not lease.request.render, priority=2)
                    assert await frontier.nack(lease, **options) == await memory.nack(expected, **options)
                case _:
                    assert await frontier.dead(lease, "failed") == await memory.dead(expected, "failed")
        elif action == "renew" and leases:
            expected, lease = leases[rng.randrange(len(leases))]
            renewed_expected, renewed = await memory.renew(expected, 10.0), await frontier.renew(lease, 10.0)
            assert (renewed and _described(renewed)) == (renewed_expected and _described(renewed_expected))
        elif action == "advance":
            clock.advance(rng.choice([1.0, 4.0, 10.0]))
        assert await frontier.counts() == await memory.counts()


WRITER = """
import asyncio, itertools, sys

from pernix import Request, Spider
from pernix.frontiers import SQLiteFrontier


class Writer(Spider):
    pass


async def main():
    frontier = SQLiteFrontier(sys.argv[1], Writer())
    for n in itertools.count():
        await frontier.add([Request(f"https://{n % 3}.example/{n}/{i}", render=i == 1) for i in range(4)])
        leases = await frontier.claim("writer", 3, 60.0)
        await frontier.ack(leases[0])
        await frontier.nack(leases[1], delay=60.0 * (n % 2))
        await frontier.dead(leases[2], "failed")
        if n == 20:
            print("writing", flush=True)


asyncio.run(main())
"""


async def test_a_process_killed_while_writing_leaves_every_request_in_one_state(opened: Opener, path: Path) -> None:
    writer = await asyncio.create_subprocess_exec(
        sys.executable, "-c", WRITER, str(path), stdout=asyncio.subprocess.PIPE
    )
    assert writer.stdout is not None
    assert await writer.stdout.readline() == b"writing\n"
    writer.kill()  # at any moment of its writing
    await writer.wait()

    with _database(path) as database:
        assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        rows = dict(database.execute("SELECT state, count(*) FROM requests GROUP BY state").fetchall())
        [(fingerprints,)] = database.execute("SELECT count(*) FROM fingerprints").fetchall()
        [(urls, distinct_urls)] = database.execute("SELECT count(url), count(DISTINCT url) FROM requests").fetchall()
        counted = dict(database.execute("SELECT state, requests FROM counts").fetchall())
        heads = database.execute("SELECT rendered, slot, id FROM heads ORDER BY rendered, slot").fetchall()
        first_ready = database.execute(
            "SELECT rendered, slot, (SELECT id FROM requests AS first WHERE first.state = 'ready' "
            "AND first.rendered = ready.rendered AND first.slot = ready.slot ORDER BY priority DESC, sequence LIMIT 1) "
            "FROM requests AS ready WHERE state = 'ready' GROUP BY rendered, slot ORDER BY rendered, slot"
        ).fetchall()
    counts = await opened().counts()

    assert urls == distinct_urls  # no Request is stored twice
    assert counted == {state: rows.get(state, 0) for state in ("ready", "delayed", "leased", "dead")} | {
        "done": fingerprints - urls  # the Requests whose Fingerprints have no Request left
    }
    assert heads == first_ready
    assert (counts.ready, counts.delayed, counts.leased, counts.done, counts.dead) == (
        counted["ready"],
        counted["delayed"],
        counted["leased"],
        counted["done"],
        counted["dead"],
    )
    assert counts.done > 20
