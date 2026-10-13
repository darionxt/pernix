from __future__ import annotations

import asyncio
import random
import time
from typing import Any, ClassVar

import pytest

from pernix import DropItem, ItemContext, Pipeline, Request, Retry
from pernix.errors import ComponentError, FeedError, RetriesExhausted
from pernix.items import PipelineStage

CTX = ItemContext(Request("https://example.com/"), None)


class Flaky(Pipeline):
    retry_times = 2
    retry_on: ClassVar[tuple[type[Exception], ...]] = (ConnectionError,)

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.calls = 0

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return item


def _stage(pipeline: object) -> tuple[PipelineStage, list[str]]:
    retries: list[str] = []
    stage = PipelineStage(pipeline, delay_scale=0, on_retry=lambda: retries.append("retry"), rng=random.Random(1))  # noqa: S311
    return stage, retries


async def test_retryable_errors_are_retried_until_the_call_succeeds() -> None:
    pipeline = Flaky([ConnectionError("reset"), Retry("deadlock")])
    stage, retries = _stage(pipeline)

    assert await stage.process({"a": 1}, CTX) == {"a": 1}

    assert (pipeline.calls, retries) == (3, ["retry", "retry"])


async def test_exhausted_retries_raise_retries_exhausted_with_the_cause() -> None:
    error = ConnectionError("down")
    pipeline = Flaky([error] * 3)
    stage, retries = _stage(pipeline)

    with pytest.raises(RetriesExhausted, match="ConnectionError: down") as raised:
        await stage.process({"a": 1}, CTX)

    assert raised.value.__cause__ is error
    assert (pipeline.calls, len(retries)) == (3, 2)


@pytest.mark.parametrize("error", [ValueError("invalid"), DropItem("duplicate")])
async def test_other_errors_and_drops_are_never_retried(error: Exception) -> None:
    pipeline = Flaky([error])
    stage, retries = _stage(pipeline)

    with pytest.raises(type(error)):
        await stage.process({"a": 1}, CTX)

    assert (pipeline.calls, retries) == (1, [])


async def test_feed_errors_are_never_retried() -> None:
    class Retries(Flaky):
        retry_on: ClassVar[tuple[type[Exception], ...]] = (Exception,)

    pipeline = Retries([FeedError("disk full")])
    stage, retries = _stage(pipeline)

    with pytest.raises(FeedError, match="disk full"):
        await stage.process({"a": 1}, CTX)

    assert (pipeline.calls, retries) == (1, [])


async def test_retry_is_exhausted_at_once_without_retry_times() -> None:
    class Once(Pipeline):
        def process(self, item: Any, ctx: ItemContext) -> Any:  # a sync process works too
            raise Retry("busy")

    stage, retries = _stage(Once())

    with pytest.raises(RetriesExhausted, match="Retry: busy"):
        await stage.process({"a": 1}, CTX)
    assert retries == []


async def test_retry_delays_follow_backoff_or_the_retry_and_are_scaled(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("pernix.items.pipeline_stage.asyncio.sleep", sleep)
    pipeline = Flaky([ConnectionError("reset"), Retry("later", delay=4.0)])
    stage = PipelineStage(pipeline, delay_scale=0.5, rng=random.Random(1))  # noqa: S311

    await stage.process({"a": 1}, CTX)

    assert 0 <= slept[0] <= 0.5  # full jitter up to 1 s, halved
    assert slept[1] == 2.0


async def test_batches_are_retried_as_a_whole() -> None:
    class Rows(Pipeline):
        batch_size = 2
        retry_times = 1

        def __init__(self) -> None:
            self.batches: list[list[Any]] = []

        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            self.batches.append(items)
            if len(self.batches) == 1:
                raise TimeoutError("slow database")

    pipeline = Rows()
    stage, _ = _stage(pipeline)

    await stage.process_batch([1, 2], [CTX, CTX])

    assert pipeline.batches == [[1, 2], [1, 2]]


async def test_retryable_errors_are_kept_as_they_are_when_retries_are_off() -> None:
    class NoRetries(Flaky):
        retry_times = 0

    stage, _ = _stage(NoRetries([ConnectionError("reset")]))

    with pytest.raises(ConnectionError, match="reset"):
        await stage.process({"a": 1}, CTX)


class Slow(Pipeline):
    retry_times = 1

    def __init__(self) -> None:
        self.calls = 0

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        self.calls += 1
        if self.calls == 1:
            await asyncio.sleep(1)
        return item


async def test_a_slow_call_times_out_and_is_retried_with_a_fresh_timeout() -> None:
    pipeline = Slow()
    stage = PipelineStage(pipeline, timeout=0.05, delay_scale=0)

    assert await stage.process({"a": 1}, CTX) == {"a": 1}
    assert pipeline.calls == 2


async def test_a_timeout_names_the_pipeline_and_the_limit() -> None:
    class Stuck(Pipeline):
        timeout = 0.02

        async def process(self, item: Any, ctx: ItemContext) -> Any:
            await asyncio.sleep(1)

    with pytest.raises(TimeoutError, match=r"Stuck took longer than 0\.02 s"):
        await PipelineStage(Stuck(), timeout=60).process({"a": 1}, CTX)


async def test_a_timeout_the_pipeline_raises_itself_is_kept() -> None:
    class OwnTimeout(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            raise TimeoutError("upstream timed out")

    with pytest.raises(TimeoutError, match="upstream timed out"):
        await PipelineStage(OwnTimeout(), timeout=5).process({"a": 1}, CTX)


async def test_sync_calls_are_not_limited() -> None:
    class Blocking(Pipeline):
        def process(self, item: Any, ctx: ItemContext) -> Any:
            time.sleep(0.05)
            return item

    assert await PipelineStage(Blocking(), timeout=0.01).process({"a": 1}, CTX) == {"a": 1}


class PlainObject:
    def process(self, item: Any, ctx: ItemContext) -> Any:
        return item


def test_plain_objects_with_process_are_pipelines() -> None:
    stage, _ = _stage(PlainObject())

    assert (stage.name, stage.batch_size) == ("PlainObject", None)
    assert stage.wants({"any": "item"})


class TakesDicts(Pipeline):
    accepts = staticmethod(lambda item: isinstance(item, dict))


class TakesTuples(Pipeline):
    accepts = (dict,)


def test_accepts_may_be_types_or_a_function() -> None:
    by_function, _ = _stage(TakesDicts())
    by_type, _ = _stage(TakesTuples())

    assert (by_function.wants({}), by_function.wants([])) == (True, False)
    assert (by_type.wants({}), by_type.wants([])) == (True, False)


class NoBatchMethod(Pipeline):
    batch_size = 10


class Nothing:
    pass


class BadOptions(Pipeline):
    def __init__(self, **options: object) -> None:
        self.__dict__.update(options)


@pytest.mark.parametrize(
    ("pipeline", "message"),
    [
        (NoBatchMethod(), "batch_size is set, so it must implement process_batch"),
        (Nothing(), "it must have process"),
        (BadOptions(batch_size=0), "batch_size must be a positive integer"),
        (BadOptions(batch_size=True), "batch_size must be a positive integer"),
        (BadOptions(batch_size=5, batch_timeout=0), "batch_timeout must be a positive number"),
        (BadOptions(retry_times=-1), "retry_times must be a non-negative integer"),
        (BadOptions(retry_on=("ConnectionError",)), "retry_on must be a tuple of exception types"),
        (BadOptions(accepts=42), "accepts must be"),
        (BadOptions(timeout=-1), "timeout must be a non-negative number"),
    ],
)
def test_pipelines_that_do_not_fit_are_rejected(pipeline: object, message: str) -> None:
    with pytest.raises(ComponentError, match=message):
        PipelineStage(pipeline)
