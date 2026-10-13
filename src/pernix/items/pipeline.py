from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pernix.items.item_adapter import ItemFilter
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["FailedItem", "ItemContext", "Pipeline"]


@dataclass(frozen=True, slots=True)
class ItemContext:
    """Where an Item came from.

    Attributes:
        request: The Request whose Callback or errback produced the Item.
        response: The Response its Callback handled; `None` for an errback's Items.
    """

    request: Request
    response: Response | None


@dataclass(frozen=True, slots=True)
class FailedItem:
    """An Item a Pipeline failed on, as the failure Pipelines receive it.

    Attributes:
        item: The Item, as the failing Pipeline received it.
        pipeline: The name of the failing Pipeline.
        error: The exception; `RetriesExhausted` once the Pipeline's retries ran out.
    """

    item: Any
    pipeline: str
    error: Exception


class Pipeline:
    """Base class for Item Pipelines, which check, clean, and store Items in order.

    Register Pipelines with the `pipelines` setting, as `{pipeline: order}`; lower
    orders run first. The Crawler builds each one like an Extension (injecting
    `Crawler`, `Settings`, `EventBus`, `Stats`, or Sections into annotated `__init__`
    parameters), opens it before the Run, and closes it after, in reverse order.

    `process()` returns the Item to pass on, which may be a new object, or `None` to
    drop it; raising `DropItem` drops it too. Any other exception is reported as
    `ItemError` and drops the Item; the Request is never fetched again for it.

    Setting `batch_size` turns on batch mode: `process_batch()` receives up to that
    many Items at once, when the batch is full, when `batch_timeout` seconds have
    passed since its first Item, and when the Run ends. The Items then pass on
    unchanged; `DropItem` drops the whole batch, and another exception fails it.
    Batches of one Pipeline are never processed concurrently.

    Each call may take `timeout` seconds, or `items.timeout`; then it is cancelled and
    fails with `TimeoutError`. Setting `retry_times` retries a call that raised one of
    `retry_on`, which include `TimeoutError` by default, or `Retry`, with full-jitter
    backoff (from 1 s, up to 60 s, scaled by `retry.delay_scale`); each attempt has
    its own timeout, and once the retries run out, the error is `RetriesExhausted`.
    Only the Pipeline's own call is repeated, so it should write idempotently, such as
    by upserting. Meanwhile the Items wait, and Callbacks are suspended once the Item
    queue fills.

    An Item a Pipeline failed on goes, as a `FailedItem`, through the failure
    Pipelines of the `failed_pipelines` setting, which are Pipelines too, without batch
    mode.

    Plain objects with `process()`, or with `batch_size` and `process_batch()`, work as
    Pipelines too; `process()` and `process_batch()` may be sync or async.

    Attributes:
        accepts: The Items this Pipeline takes, as types or a function
            `(item) -> bool`; others skip it. `None` takes every Item.
        batch_size: Items per batch, enabling batch mode; `None` processes Items one
            by one.
        batch_timeout: Seconds a batch waits for more Items after its first one.
        retry_times: Retries of a call that raised a retryable error; 0 never retries.
        retry_on: The exceptions that are retried, besides `Retry`.
        timeout: Seconds a call may take; `None` takes `items.timeout`, and 0 sets no
            limit. A sync call cannot be cancelled, so it is never limited.

    Example:
        >>> class Clean(Pipeline):
        ...     async def process(self, item, ctx):
        ...         if not item["title"]:
        ...             raise DropItem("empty title")
        ...         return item
    """

    accepts: ClassVar[ItemFilter | None] = None
    batch_size: ClassVar[int | None] = None
    batch_timeout: ClassVar[float] = 1.0
    retry_times: ClassVar[int] = 0
    retry_on: ClassVar[tuple[type[Exception], ...]] = (OSError, TimeoutError)
    timeout: ClassVar[float | None] = None

    async def open(self) -> None:
        """Acquire resources before the Run starts. Does nothing by default."""

    async def close(self) -> None:
        """Release resources after the Run ends. Does nothing by default."""

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        """Process one Item.

        Args:
            item: The Item, as the previous Pipeline passed it on.
            ctx: Where the Item came from.

        Returns:
            The Item to pass on, or `None` to drop it. By default, `item`.
        """
        return item

    async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
        """Process a batch of Items, in batch mode.

        Args:
            items: The Items, in the order they arrived.
            contexts: Where each Item came from, in the same order.

        Raises:
            NotImplementedError: The subclass set `batch_size` without implementing it.
        """
        raise NotImplementedError(f"{type(self).__name__} sets batch_size but does not implement process_batch()")
