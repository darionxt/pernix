"""Kept pages: the pages rendered fetches keep open for the Callback that works with them, until it ends.

A Run handles each Request inside `keeping_pages()`: a fetch with
`Render(keep_page=True)` hands its page to it, open, and the scope closes it, with
its context, once the Request's Callback ends, however it ends. A fetch outside
such a scope, or inside `without_keeping()`, as an Inline Fetch is, cannot keep its
page. While a page is kept, the rendered fetches made in the scope, those of tasks
it starts included, share the kept page's slot rather than wait for another, as a
Callback holding a page could otherwise wait forever for one.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Generator

    from patchright.async_api import Page

__all__ = ["KeptPage", "PageKeeping", "keeping_pages", "page_keeping", "sharing_a_kept_slot", "without_keeping"]


class KeptPage:
    """A page a fetch keeps open past it, with what it holds: its page slot, its context, and its browser.

    Attributes:
        page: The page.
    """

    def __init__(self, page: Page, *, close: Callable[[], None], broken: Callable[[], Exception | None]) -> None:
        """Keep a page.

        Args:
            page: The page.
            close: Starts closing the page and what it holds, without waiting.
            broken: Tells why the page went away while kept, as it crashed or its
                browser was lost: the error the fetch would have failed with, or
                `None`.
        """
        self.page = page
        self._close: Callable[[], None] | None = close
        self._broken = broken

    def broken(self) -> Exception | None:
        """Why the page went away while kept, if it did: a `PageCrashed` or a `BrowserLost`."""
        return self._broken()

    def close(self) -> None:
        """Start closing the page, its context, and releasing its slot; closing it again does nothing."""
        close, self._close = self._close, None
        if close is not None:
            close()


class PageKeeping:
    """The pages kept for the Callback of one Request, closed together once it ends.

    Attributes:
        pages: The pages kept, in the order kept.
    """

    def __init__(self) -> None:
        """Keep no page yet."""
        self.pages: list[KeptPage] = []

    def keep(self, page: KeptPage) -> None:
        """Keep a page until the scope ends.

        Args:
            page: The page.
        """
        self.pages.append(page)

    def failure(self, error: Exception) -> Exception | None:
        """The error a Callback failed with in place of its own, when a page it kept went away meanwhile.

        Args:
            error: What the Callback raised.

        Returns:
            The `PageCrashed` or `BrowserLost` of the first page kept that went away,
            caused by `error`; `None` if none did.
        """
        for page in self.pages:
            broken = page.broken()
            if broken is not None:
                broken.__cause__ = error
                return broken
        return None

    def close(self) -> None:
        """Start closing every page kept."""
        for page in self.pages:
            page.close()


_keeping: ContextVar[PageKeeping | None] = ContextVar("pernix_page_keeping", default=None)
"""Where the fetches of the Request being handled keep their pages, if anywhere."""

_refused: ContextVar[bool] = ContextVar("pernix_page_keeping_refused", default=False)
"""Whether the fetches inside cannot keep their pages, as those of an Inline Fetch cannot."""


@asynccontextmanager
async def keeping_pages() -> AsyncGenerator[PageKeeping]:
    """Let the fetches inside keep their pages open until the block ends, then close those pages.

    Yields:
        The pages kept, as they are kept.
    """
    keeping = PageKeeping()
    token = _keeping.set(keeping)
    try:
        yield keeping
    finally:
        _keeping.reset(token)
        keeping.close()  # in tasks of their own, so a cancelled block still closes them


@contextmanager
def without_keeping() -> Generator[None]:
    """Keep the fetches inside from keeping their pages, while they still share the slot of a page kept.

    Yields:
        Nothing; the block runs with page keeping refused.
    """
    token = _refused.set(True)
    try:
        yield
    finally:
        _refused.reset(token)


def page_keeping() -> PageKeeping | None:
    """Where a fetch made now may keep its page; `None` where it cannot."""
    return None if _refused.get() else _keeping.get()


def sharing_a_kept_slot() -> bool:
    """Whether a rendered fetch made now shares the page slot of a page kept, rather than take one of its own."""
    keeping = _keeping.get()
    return keeping is not None and bool(keeping.pages)
