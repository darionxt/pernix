from __future__ import annotations

import asyncio
import inspect
import logging
import random
from typing import TYPE_CHECKING, Any, cast

from pernix.errors import ComponentError, DropItem, RetriesExhausted, Retry
from pernix.items.item_adapter import item_matcher
from pernix.items.pipeline import Pipeline
from pernix.retry_policy import full_jitter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from pernix.items.pipeline import ItemContext

__all__ = ["PipelineStage"]

logger = logging.getLogger(__name__)

_BACKOFF_BASE = 1.0
_BACKOFF_MAX = 60.0


class PipelineStage:
    """One Pipeline as Items go through it: which Items it takes, and how its calls are retried.

    Attributes:
        pipeline: The Pipeline.
        name: Names the Pipeline in logs and Events.
        batch_size: Items per batch, or `None` outside batch mode.
        batch_timeout: Seconds a batch waits for more Items after its first one.
    """

    def __init__(
        self,
        pipeline: object,
        *,
        timeout: float = 0.0,
        batches: bool = True,
        delay_scale: float = 1.0,
        on_retry: Callable[[], None] = lambda: None,
        rng: random.Random | None = None,
    ) -> None:
        """Check a Pipeline and read its options.

        Args:
            pipeline: A `Pipeline`, or an object with `process()`, or with
                `batch_size` and `process_batch()`.
            timeout: Seconds a call may take unless the Pipeline sets its own
                `timeout`; 0 for no limit.
            batches: Whether the Pipeline may use batch mode.
            delay_scale: Multiplies every retry delay.
            on_retry: Called before each retry, for the stats.
            rng: The source of backoff jitter.

        Raises:
            pernix.errors.ComponentError: The Pipeline cannot process Items, or an
                option is invalid.
        """
        self.pipeline = pipeline
        self.name = type(pipeline).__qualname__
        try:
            self.batch_size = _batch_size(pipeline)
            self.batch_timeout = _batch_timeout(pipeline)
            self._retry_times = _retry_times(pipeline)
            self._retry_on = _retry_on(pipeline)
            self._timeout = _timeout(pipeline, timeout)
            self._wants = item_matcher(_class_attribute(pipeline, "accepts"))
        except (TypeError, ValueError) as exc:
            raise ComponentError(f"pipeline {self.name}: {exc}") from exc
        if self.batch_size is not None and not batches:
            raise ComponentError(
                f"pipeline {self.name}: failure Pipelines process one Item at a time; unset batch_size"
            )
        if self.batch_size is not None and not _implements(pipeline, "process_batch"):
            raise ComponentError(f"pipeline {self.name}: batch_size is set, so it must implement process_batch()")
        if self.batch_size is None and not callable(getattr(pipeline, "process", None)):
            raise ComponentError(
                f"pipeline {self.name}: it must have process(item, ctx), or batch_size and process_batch(items, contexts)"
            )
        self._delay_scale = delay_scale
        self._on_retry = on_retry
        self._rng = rng or random.Random()  # noqa: S311  # jitter, not cryptography

    def wants(self, item: object) -> bool:
        """Tell whether the Pipeline takes this Item."""
        return self._wants(item)

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        """Call `process()`, retrying as configured.

        Raises:
            DropItem: The Pipeline dropped the Item.
            RetriesExhausted: A retryable error persisted through every retry.
            Exception: The Pipeline raised an error that is not retried.
        """
        method: Any = getattr(self.pipeline, "process")  # noqa: B009  # checked in __init__
        return await self._retrying(lambda: method(item, ctx))

    async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
        """Call `process_batch()`, retrying as configured.

        Raises:
            DropItem: The Pipeline dropped the batch.
            RetriesExhausted: A retryable error persisted through every retry.
            Exception: The Pipeline raised an error that is not retried.
        """
        method: Any = getattr(self.pipeline, "process_batch")  # noqa: B009  # checked in __init__
        await self._retrying(lambda: method(items, contexts))

    async def _retrying(self, call: Callable[[], object]) -> Any:
        attempt = 1
        while True:
            try:
                return await self._call(call)
            except DropItem:
                raise
            except Exception as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                if attempt > self._retry_times:
                    if attempt == 1 and not isinstance(exc, Retry):
                        raise  # retries are off: keep the error as it is
                    raise RetriesExhausted(f"{type(exc).__name__}: {exc}") from exc
                logger.warning(
                    "Retrying %s in %.2f s after attempt %d: %s: %s", self.name, delay, attempt, type(exc).__name__, exc
                )
                self._on_retry()
                await asyncio.sleep(delay)
                attempt += 1

    async def _call(self, call: Callable[[], object]) -> Any:
        """Make one call, cancelling it once it takes longer than the timeout."""
        result = call()
        if not inspect.isawaitable(result):
            return result
        if not self._timeout:
            return await result
        scope = asyncio.timeout(self._timeout)
        try:
            async with scope:
                return await result
        except TimeoutError:
            if scope.expired():
                raise TimeoutError(f"{self.name} took longer than {self._timeout:g} s") from None
            raise

    def _retry_delay(self, error: Exception, attempt: int) -> float | None:
        """Seconds before retrying after `error`, or `None` if it is not retried."""
        if isinstance(error, Retry) and error.delay is not None:
            return error.delay * self._delay_scale
        if not isinstance(error, (Retry, *self._retry_on)):
            return None
        return full_jitter(attempt, base=_BACKOFF_BASE, cap=_BACKOFF_MAX, rng=self._rng) * self._delay_scale


def _batch_size(pipeline: object) -> int | None:
    value: object = getattr(pipeline, "batch_size", None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"batch_size must be a positive integer or None, got {value!r}")
    return value


def _batch_timeout(pipeline: object) -> float:
    value: object = getattr(pipeline, "batch_timeout", 1.0)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"batch_timeout must be a positive number of seconds, got {value!r}")
    return float(value)


def _retry_times(pipeline: object) -> int:
    value: object = getattr(pipeline, "retry_times", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"retry_times must be a non-negative integer, got {value!r}")
    return value


def _retry_on(pipeline: object) -> tuple[type[Exception], ...]:
    value: object = getattr(pipeline, "retry_on", ())
    kinds = tuple(cast("Iterable[object]", value)) if isinstance(value, tuple | list) else (value,)
    if not all(isinstance(kind, type) and issubclass(kind, Exception) for kind in kinds):
        raise ValueError(f"retry_on must be a tuple of exception types, got {value!r}")
    return cast("tuple[type[Exception], ...]", kinds)


def _timeout(pipeline: object, default: float) -> float:
    value: object = getattr(pipeline, "timeout", None)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ValueError(f"timeout must be a non-negative number of seconds or None, got {value!r}")
    return float(value)


def _class_attribute(component: object, name: str) -> Any:
    """An option of a component, read so that a plain function stays unbound."""
    instance_values: dict[str, Any] = getattr(component, "__dict__", {})
    if name in instance_values:
        return instance_values[name]
    return getattr(type(component), name, None)


def _implements(component: object, name: str) -> bool:
    method = getattr(type(component), name, None)
    return callable(method) and method is not getattr(Pipeline, name, None)
