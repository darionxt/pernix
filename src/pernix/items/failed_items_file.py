from __future__ import annotations

from typing import TYPE_CHECKING

from pernix.feeds.feed import Feed, render_path
from pernix.items.item_adapter import is_item, item_to_dict
from pernix.items.pipeline import FailedItem, Pipeline
from pernix.settings import FeedOptions, ItemSettings

if TYPE_CHECKING:
    from pernix.crawler import Crawler
    from pernix.items.pipeline import ItemContext

__all__ = ["FailedItemsFile"]


class FailedItemsFile(Pipeline):
    """Writes each `FailedItem` to the JSON-lines file of `items.failed_output`.

    Each line holds the failing `pipeline`, the `error`, the `url` of the Request, and
    the `item`, so the Items can be processed again later. The file is a Feed: it
    appears complete when the Run ends. Without `items.failed_output`, it does
    nothing. Enabled by default in the `failed_pipelines` setting; a failure to write
    ends the Run.
    """

    def __init__(self, crawler: Crawler) -> None:
        """Read `items.failed_output`.

        Args:
            crawler: The Crawler of the Run.
        """
        template = crawler.settings.section(ItemSettings).failed_output
        self._feed: Feed | None = None
        if template is not None:
            path = render_path(template, name=crawler.spider.name, run_id=crawler.run_id, started=crawler.started_at)
            self._feed = Feed(path, FeedOptions(format="jsonl"))

    async def open(self) -> None:
        """Create the file, if one is configured."""
        if self._feed is not None:
            self._feed.open()

    async def close(self) -> None:
        """Move the finished file into place."""
        if self._feed is not None:
            self._feed.close()

    async def process(self, item: FailedItem, ctx: ItemContext) -> FailedItem:
        """Write one failed Item and pass it on.

        Raises:
            pernix.errors.FeedError: The file cannot be written.
        """
        if self._feed is not None:
            self._feed.write(
                {
                    "pipeline": item.pipeline,
                    "error": f"{type(item.error).__name__}: {item.error}",
                    "url": ctx.request.url,
                    "item": item_to_dict(item.item) if is_item(item.item) else repr(item.item),
                }
            )
        return item
