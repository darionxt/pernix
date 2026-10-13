from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import DropItem, ItemContext, Pipeline, Request, events
from pernix.errors import ComponentError, FeedError
from pernix.feeds import Feed
from pernix.items import FailedItem, ItemProcessor
from pernix.settings import FeedOptions
from pernix.stats import Stats

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

CTX = ItemContext(Request("https://example.com/page"), None)


class Recorder:
    """Records the Item Events of a processing as short tuples."""

    def __init__(self, bus: events.EventBus) -> None:
        self.log: list[tuple[Any, ...]] = []
        bus.on(events.ItemScraped, lambda event: self.log.append(("scraped", event.item)))
        bus.on(events.ItemDropped, lambda event: self.log.append(("dropped", event.item, event.reason)))
        bus.on(events.ItemError, lambda event: self.log.append(("error", event.item, type(event.error).__name__)))


def _processor(
    pipelines: Sequence[object],
    *,
    feeds: Sequence[Feed] = (),
    failed_pipelines: Sequence[object] = (),
    queue_size: int = 100,
    concurrency: int = 1,
    timeout: float = 0.0,
    stats: Stats | None = None,
) -> tuple[ItemProcessor, Recorder]:
    bus = events.EventBus(timeout=5)
    processor = ItemProcessor(
        pipelines,
        feeds,
        bus,
        stats or Stats(),
        failed_pipelines=failed_pipelines,
        queue_size=queue_size,
        concurrency=concurrency,
        timeout=timeout,
        delay_scale=0,
    )
    return processor, Recorder(bus)


async def _process(pipelines: Sequence[object], items: Sequence[Any], **options: Any) -> list[tuple[Any, ...]]:
    processor, recorder = _processor(pipelines, **options)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        for item in items:
            await processor.put(item, CTX)
        await processor.join()
    return recorder.log


class Tag(Pipeline):
    def __init__(self, tag: str) -> None:
        self.tag = tag

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        return {**item, "tags": [*item.get("tags", []), self.tag]}


async def test_items_pass_the_pipelines_in_order() -> None:
    class Upper:
        def process(self, item: Any, ctx: ItemContext) -> Any:  # a plain object, sync
            return {**item, "n": item["n"] * 10}

    log = await _process([Tag("a"), Upper(), Tag("b")], [{"n": 1}, {"n": 2}])

    assert log == [("scraped", {"n": 10, "tags": ["a", "b"]}), ("scraped", {"n": 20, "tags": ["a", "b"]})]


class Drop(Pipeline):
    async def process(self, item: Any, ctx: ItemContext) -> Any:
        match item["n"]:
            case 1:
                return None
            case 2:
                raise DropItem("duplicate")
            case 3:
                raise DropItem
            case 4:
                raise ValueError("bad price")
            case 5:
                return "not an item"
            case _:
                return item


async def test_dropped_and_failed_items_go_no_further() -> None:
    after = Tag("after")

    log = await _process([Drop(), after], [{"n": n} for n in range(1, 7)])

    assert log == [
        ("dropped", {"n": 1}, "Drop returned None"),
        ("dropped", {"n": 2}, "duplicate"),
        ("dropped", {"n": 3}, "dropped by Drop"),
        ("error", {"n": 4}, "ValueError"),
        ("error", {"n": 5}, "TypeError"),
        ("scraped", {"n": 6, "tags": ["after"]}),
    ]


async def test_pipelines_skip_the_items_they_do_not_accept() -> None:
    class OnlyBooks(Tag):
        accepts = staticmethod(lambda item: "isbn" in item)

    log = await _process([OnlyBooks("book")], [{"isbn": "1"}, {"name": "author"}])

    assert log == [("scraped", {"isbn": "1", "tags": ["book"]}), ("scraped", {"name": "author"})]


class Rows(Pipeline):
    batch_size = 2
    batch_timeout = 60.0

    def __init__(self, fail_on: object = None) -> None:
        self.batches: list[list[Any]] = []
        self.contexts: list[list[ItemContext]] = []
        self.fail_on = fail_on

    async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
        self.batches.append([item["n"] for item in items])
        self.contexts.append(contexts)
        if self.fail_on is not None and self.fail_on in self.batches[-1]:
            raise DropItem("rejected batch") if self.fail_on == 1 else ConnectionAbortedError("lost connection")


async def test_batches_are_processed_when_full_and_at_the_end_then_pass_on() -> None:
    rows = Rows()

    log = await _process([rows, Tag("after")], [{"n": n} for n in range(1, 6)])

    assert rows.batches == [[1, 2], [3, 4], [5]]
    assert rows.contexts[0] == [CTX, CTX]
    assert log == [("scraped", {"n": n, "tags": ["after"]}) for n in range(1, 6)]


async def test_a_full_batch_is_processed_at_once() -> None:
    rows = Rows()
    processor, recorder = _processor([rows])
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        for n in (1, 2, 3):
            await processor.put({"n": n}, CTX)
        await asyncio.sleep(0.05)
        assert rows.batches == [[1, 2]]  # before join(), and long before the timeout
        assert recorder.log == [("scraped", {"n": 1}), ("scraped", {"n": 2})]
        await processor.join()

    assert rows.batches == [[1, 2], [3]]


async def test_batches_are_processed_once_their_timeout_passes() -> None:
    class Quick(Rows):
        batch_size = 100
        batch_timeout = 0.05

    rows = Quick()
    processor, recorder = _processor([rows])
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        await processor.put({"n": 1}, CTX)
        await processor.put({"n": 2}, CTX)
        await asyncio.sleep(0.2)
        assert rows.batches == [[1, 2]]  # before join()
        assert [entry[0] for entry in recorder.log] == ["scraped", "scraped"]
        await processor.join()

    assert rows.batches == [[1, 2]]


@pytest.mark.parametrize(
    ("fail_on", "outcome"),
    [(1, ("dropped", "rejected batch")), (3, ("error", "RetriesExhausted"))],
)
async def test_a_failed_batch_drops_or_fails_each_of_its_items(fail_on: int, outcome: tuple[str, str]) -> None:
    class Retrying(Rows):
        retry_times = 1
        retry_on: ClassVar[tuple[type[Exception], ...]] = (ConnectionError,)

    rows = Retrying(fail_on=fail_on)

    log = await _process([rows], [{"n": n} for n in range(1, 5)])

    failed = [1, 2] if fail_on == 1 else [3, 4]
    assert sorted(log, key=lambda entry: entry[1]["n"]) == [
        (outcome[0], {"n": n}, outcome[1]) if n in failed else ("scraped", {"n": n}) for n in range(1, 5)
    ]


class OddRows(Rows):
    accepts = staticmethod(lambda item: item["n"] % 2 == 1)


async def test_items_a_batch_pipeline_does_not_accept_keep_their_place() -> None:
    rows = OddRows()

    log = await _process([rows, Tag("after")], [{"n": n} for n in range(1, 6)])

    assert rows.batches == [[1, 3], [5]]
    assert log == [("scraped", {"n": n, "tags": ["after"]}) for n in range(1, 6)]


async def test_items_keep_their_place_behind_a_failed_batch() -> None:
    class Failing(OddRows):
        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            raise ValueError("bad batch")

    log = await _process([Failing()], [{"n": n} for n in (1, 2, 3, 4)])

    assert log == [
        ("error", {"n": 1}, "ValueError"),
        ("scraped", {"n": 2}),
        ("error", {"n": 3}, "ValueError"),
        ("scraped", {"n": 4}),
    ]


async def test_items_wait_behind_a_batch_being_processed() -> None:
    release = asyncio.Event()

    class Slow(OddRows):
        batch_size = 100
        batch_timeout = 0.01

        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            await release.wait()

    processor, recorder = _processor([Slow()])
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        await processor.put({"n": 1}, CTX)
        await asyncio.sleep(0.05)  # the timeout starts processing 1, which waits
        await processor.put({"n": 2}, CTX)
        await asyncio.sleep(0.05)
        assert recorder.log == []  # 2 does not overtake 1
        release.set()
        await processor.join()

    assert recorder.log == [("scraped", {"n": 1}), ("scraped", {"n": 2})]


async def test_items_waiting_behind_a_batch_are_bounded() -> None:
    class Rare(Rows):
        batch_size = 100
        accepts = staticmethod(lambda item: item["n"] == 0)

    rows = Rare()
    processor, recorder = _processor([rows], queue_size=1)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        for n in range(301):
            await processor.put({"n": n}, CTX)
        await asyncio.sleep(0.05)
        assert rows.batches == [[0]]  # processed small, long before the batch timeout
        assert len(recorder.log) >= 299  # only what waits behind the batch is not delivered yet
        await processor.join()

    assert [entry[1]["n"] for entry in recorder.log] == list(range(301))


async def test_a_feed_failure_after_a_batch_is_reported_once(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "items.jsonl")
    feed.open()
    processor, recorder = _processor([Rows()], feeds=[feed])
    try:
        with pytest.RaisesGroup(FeedError):
            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                processor.start(tasks)
                await processor.put({"n": 1, "raw": b"\x00"}, CTX)
                await processor.put({"n": 2}, CTX)  # fills the batch; 1 then fails to be written
                await processor.join()
    finally:
        feed.close()

    assert recorder.log == [("error", {"n": 1, "raw": b"\x00"}, "FeedError")]


async def test_a_feed_error_in_a_pipeline_is_never_retried_and_ends_the_processing(tmp_path: Path) -> None:
    class Writer(Pipeline):
        retry_times = 3
        retry_on: ClassVar[tuple[type[Exception], ...]] = (Exception,)

        def __init__(self) -> None:
            self.calls = 0
            self.feed = Feed(tmp_path / "rows.jsonl")

        async def process(self, item: Any, ctx: ItemContext) -> Any:
            self.calls += 1
            self.feed.write(item)
            return item

    writer = Writer()
    writer.feed.open()
    processor, recorder = _processor([writer])
    try:
        with pytest.RaisesGroup(FeedError):
            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                processor.start(tasks)
                await processor.put({"raw": b"\x00"}, CTX)
                await processor.join()
    finally:
        writer.feed.close()

    assert writer.calls == 1
    assert recorder.log == [("error", {"raw": b"\x00"}, "FeedError")]


async def test_batches_of_one_pipeline_never_overlap() -> None:
    class Slow(Pipeline):
        batch_size = 2

        def __init__(self) -> None:
            self.active = 0
            self.most = 0

        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            self.active += 1
            self.most = max(self.most, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1

    slow = Slow()

    await _process([slow], [{"n": n} for n in range(8)], concurrency=4)

    assert slow.most == 1


async def test_a_slow_pipeline_suspends_the_producer_once_the_queue_is_full() -> None:
    release = asyncio.Event()

    class Blocked(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            await release.wait()
            return item

    processor, recorder = _processor([Blocked()], queue_size=2)
    produced: list[int] = []

    async def produce() -> None:
        for n in range(10):
            await processor.put({"n": n}, CTX)
            produced.append(n)

    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        producer = tasks.create_task(produce())
        await asyncio.sleep(0.05)
        assert produced == [0, 1, 2]  # the worker holds 0; 1 and 2 fill the queue; 3 waits
        assert not producer.done()
        release.set()
        await producer
        await processor.join()

    assert produced == list(range(10))
    assert len(recorder.log) == 10


async def test_concurrent_workers_process_items_at_once() -> None:
    class Sleepy(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            await asyncio.sleep(0.1)
            return item

    loop = asyncio.get_running_loop()
    started = loop.time()

    log = await _process([Sleepy()], [{"n": n} for n in range(4)], concurrency=4)

    assert loop.time() - started < 0.3
    assert sorted(entry[1]["n"] for entry in log) == [0, 1, 2, 3]


async def test_items_reach_the_feeds_that_accept_them(tmp_path: Path) -> None:
    every = Feed(tmp_path / "all.jsonl")
    books = Feed(tmp_path / "books.jsonl", FeedOptions(accepts=lambda item: "isbn" in item))
    every.open()
    books.open()

    await _process([], [{"isbn": "1"}, {"name": "author"}], feeds=[every, books])
    every.close()
    books.close()

    assert (tmp_path / "all.jsonl").read_text() == '{"isbn":"1"}\n{"name":"author"}\n'
    assert (tmp_path / "books.jsonl").read_text() == '{"isbn":"1"}\n'


class Collect(Pipeline):
    def __init__(self, stop: bool = False) -> None:
        self.failed: list[tuple[FailedItem, ItemContext]] = []
        self.stop = stop

    async def process(self, item: FailedItem, ctx: ItemContext) -> FailedItem | None:
        self.failed.append((item, ctx))
        return None if self.stop else item


async def test_failed_items_go_through_the_failure_pipelines_in_order() -> None:
    first, second, skipped = Collect(), Collect(stop=True), Collect()

    log = await _process([Drop()], [{"n": 2}, {"n": 4}, {"n": 6}], failed_pipelines=[first, second, skipped])

    assert [entry[0] for entry in log] == ["dropped", "error", "scraped"]  # drops are not failures
    [(failed, ctx)] = first.failed
    assert (failed.item, failed.pipeline, type(failed.error), ctx) == ({"n": 4}, "Drop", ValueError, CTX)
    assert [failed for failed, _ in second.failed] == [failed]
    assert skipped.failed == []  # the second one stopped it


async def test_a_failure_pipeline_that_fails_loses_the_failed_item(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(Pipeline):
        async def process(self, item: FailedItem, ctx: ItemContext) -> FailedItem:
            raise ConnectionRefusedError("dead letter queue down")

    after = Collect()
    stats = Stats()

    log = await _process([Drop()], [{"n": 4}, {"n": 6}], failed_pipelines=[Broken(), after], stats=stats)

    assert [entry[0] for entry in log] == ["error", "scraped"]
    assert after.failed == []
    assert stats.get("items.failures_lost") == 1
    assert "Broken lost a failed Item from <Request GET https://example.com/page>" in caplog.text


async def test_failure_pipelines_have_no_batch_mode() -> None:
    class Batched(Rows):
        pass

    with pytest.raises(ComponentError, match="failure Pipelines process one Item at a time"):
        _processor([], failed_pipelines=[Batched()])


async def test_a_hanging_pipeline_times_out_and_the_items_move_on() -> None:
    class Hangs(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            if item["n"] == 1:
                await asyncio.Event().wait()
            return item

    collect = Collect()

    log = await _process([Hangs()], [{"n": 1}, {"n": 2}], timeout=0.05, failed_pipelines=[collect])

    assert log == [("error", {"n": 1}, "TimeoutError"), ("scraped", {"n": 2})]
    assert str(collect.failed[0][0].error).endswith("Hangs took longer than 0.05 s")


async def test_a_pipeline_may_set_its_own_timeout() -> None:
    class Patient(Pipeline):
        timeout = 0  # no limit, whatever items.timeout says

        async def process(self, item: Any, ctx: ItemContext) -> Any:
            await asyncio.sleep(0.15)
            return item

    log = await _process([Patient()], [{"n": 1}], timeout=0.05)

    assert log == [("scraped", {"n": 1})]


async def test_a_batch_that_times_out_fails_each_of_its_items() -> None:
    class SlowRows(Rows):
        timeout = 0.05

        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            await asyncio.sleep(1)

    log = await _process([SlowRows()], [{"n": 1}, {"n": 2}])

    assert log == [("error", {"n": 1}, "TimeoutError"), ("error", {"n": 2}, "TimeoutError")]


async def test_a_feed_that_fails_to_write_fails_the_processing(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "items.jsonl")
    feed.open()
    processor, recorder = _processor([], feeds=[feed])

    try:
        with pytest.RaisesGroup(pytest.RaisesExc(FeedError, match="not JSON serializable: bytes")):
            async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
                processor.start(tasks)
                await processor.put({"raw": b"\x00"}, CTX)
                await processor.join()
    finally:
        feed.close()

    assert recorder.log == [("error", {"raw": b"\x00"}, "FeedError")]


async def test_pipeline_retries_are_counted() -> None:
    class FailsOnce(Pipeline):
        retry_times = 2

        def __init__(self) -> None:
            self.calls = 0

        async def process(self, item: Any, ctx: ItemContext) -> Any:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("slow")
            return item

    stats = Stats()

    log = await _process([FailsOnce()], [{"n": 1}], stats=stats)

    assert log == [("scraped", {"n": 1})]
    assert stats.get("items.retried") == 1


def test_queue_size_and_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ItemProcessor([], [], events.EventBus(), Stats(), queue_size=0)


async def test_the_outlet_receives_the_items_that_pass_before_they_are_reported(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "items.jsonl")
    bus = events.EventBus(timeout=5)
    recorder = Recorder(bus)

    async def outlet(item: Any) -> None:
        recorder.log.append(("outlet", item))

    processor = ItemProcessor([Tag("a"), Drop()], [feed], bus, Stats(), outlet=outlet)
    feed.open()
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        for n in (1, 6):
            await processor.put({"n": n}, CTX)
        await processor.join()
    feed.close()

    passed = {"n": 6, "tags": ["a"]}
    assert recorder.log == [
        ("dropped", {"n": 1, "tags": ["a"]}, "Drop returned None"),
        ("outlet", passed),
        ("scraped", passed),
    ]
    assert (tmp_path / "items.jsonl").read_text() == '{"n":6,"tags":["a"]}\n'


async def test_a_waiting_outlet_holds_the_items_behind() -> None:
    gate = asyncio.Event()
    taken: list[Any] = []

    async def outlet(item: Any) -> None:
        await gate.wait()
        taken.append(item)

    processor = ItemProcessor([], [], events.EventBus(timeout=5), Stats(), outlet=outlet, queue_size=1)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        await processor.put({"n": 1}, CTX)
        await processor.put({"n": 2}, CTX)  # the worker holds 1, the queue holds 2
        third = asyncio.create_task(processor.put({"n": 3}, CTX))
        await asyncio.sleep(0.01)
        assert (taken, third.done()) == ([], False)
        gate.set()
        await third
        await processor.join()

    assert taken == [{"n": 1}, {"n": 2}, {"n": 3}]


async def test_items_not_processed_when_the_processing_is_cancelled_are_discarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()

    class Stuck(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            if item["n"] == 2:
                started.set()
                await asyncio.Event().wait()
            return item

    stats = Stats()
    processor, recorder = _processor([Stuck()], stats=stats)

    async def process() -> None:
        async with asyncio.TaskGroup() as tasks:
            processor.start(tasks)
            for n in (1, 2, 3, 4):
                await processor.put({"n": n}, CTX)
            await processor.join()

    work = asyncio.create_task(process())
    async with asyncio.timeout(5):
        await started.wait()
        await asyncio.sleep(0)  # 3 and 4 are queued behind 2
        work.cancel()
        await asyncio.wait([work])

    assert recorder.log == [("scraped", {"n": 1})]
    assert processor.discard() == 3
    assert processor.discard() == 0
    assert stats.get("items.discarded") == 3
    assert "Discarded 3 Items the Run stopped before they were processed" in caplog.text


async def test_nothing_is_discarded_after_a_complete_processing() -> None:
    stats = Stats()
    processor, _ = _processor([Drop()], stats=stats)
    async with asyncio.timeout(5), asyncio.TaskGroup() as tasks:
        processor.start(tasks)
        for n in (1, 2, 3):
            await processor.put({"n": n}, CTX)
        await processor.join()

    assert processor.discard() == 0
    assert stats.get("items.discarded") is None
