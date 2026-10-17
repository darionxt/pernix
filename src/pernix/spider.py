from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from pernix.account import read_accounts
from pernix.crawler import Crawler
from pernix.request import Request
from pernix.runner import run_spider
from pernix.settings import SessionSettings
from pernix.spider_arguments import check_argument_names
from pernix.spider_hooks import base_hook

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterator, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from pernix.account import Account
    from pernix.fetchers import StreamedResponse
    from pernix.items import ItemStream
    from pernix.request import CallbackResult
    from pernix.response import Response
    from pernix.run_result import RunResult
    from pernix.session import Session
    from pernix.stats import Stats

__all__ = ["Spider"]


class Spider:
    """Base class for user crawling logic.

    Subclasses set `start_urls` or override `start()`, and implement `parse()` and any
    other Callbacks as async generators that yield Requests and Items.

    A Spider takes arguments through the keyword parameters of its `__init__`. Each
    Run creates a new Spider object with them, converting strings, such as those from
    a command line, to the annotated types; `__init__` may raise `ValueError` to
    reject them. `settings` and `output` cannot name an argument.

    With a Session Pool, `accounts()` gives the Accounts to bind Sessions to,
    `login()` logs Sessions in, and `is_logged_out()` tells when one must log in again.

    Attributes:
        name: Identifies the Spider in logs; defaults to the lower-cased class name.
        start_urls: URLs fetched with a GET when `start()` is not overridden.
        allowed_domains: Domains, such as `"example.com"`, whose hosts and subdomains
            Requests may go to; Requests elsewhere are dropped as offsite. Empty allows
            every host.
        settings: Settings for this Spider, by dotted key; they override the project
            file and the environment, and are overridden by run arguments.
        crawler: The Crawler running this Spider.
        stats: The stats of the Run.
        logger: The logger `pernix.spiders.<name>`.

    Example:
        >>> class Titles(Spider):
        ...     start_urls = ["https://example.com/"]
        ...
        ...     async def parse(self, response):
        ...         yield {"title": response.css("title::text").get()}
    """

    name: ClassVar[str] = ""
    start_urls: ClassVar[Sequence[str]] = ()
    allowed_domains: ClassVar[Sequence[str]] = ()
    settings: ClassVar[Mapping[str, Any]] = MappingProxyType({})
    _crawler: Crawler | None = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not cls.name:
            cls.name = cls.__name__.lower()
        check_argument_names(cls)

    @property
    def crawler(self) -> Crawler:
        """The Crawler running this Spider; the Crawler sets it when it is created.

        Raises:
            RuntimeError: No Crawler was created for this Spider object yet.
        """
        if self._crawler is None:
            raise RuntimeError(f"{type(self).__name__} has no Crawler yet; one is set when a Crawler is created for it")
        return self._crawler

    @crawler.setter
    def crawler(self, crawler: Crawler) -> None:
        if self._crawler is not None and self._crawler is not crawler:
            raise RuntimeError(
                f"this {type(self).__name__} object already belongs to another Crawler; create one Spider object per Run"
            )
        self._crawler = crawler

    @property
    def stats(self) -> Stats:
        """The stats of the Run, as `self.crawler.stats`; add your own under your own prefix.

        Raises:
            RuntimeError: No Crawler was created for this Spider object yet.
        """
        return self.crawler.stats

    @property
    def logger(self) -> logging.Logger:
        """The logger `pernix.spiders.<name>`; its records carry the ids of the Run and the Request."""
        return logging.getLogger(f"pernix.spiders.{self.name}")

    async def start(self) -> AsyncIterator[Request]:
        """Yield the initial Requests.

        Yields:
            One GET Request per URL in `start_urls`, unless overridden.
        """
        for url in self.start_urls:
            yield Request(url)

    async def on_idle(self) -> AsyncIterator[Request]:
        """Yield more Requests once the Frontier is idle; the Run ends when none are added.

        It runs whenever nothing is ready, delayed, or being processed, after the
        `SpiderIdle` Event. The Run ends once it adds no new Request to the Frontier,
        including when everything it yields is a duplicate or vetoed. As in `start()`,
        URLs must be absolute.

        Yields:
            Nothing, unless overridden.
        """
        return
        yield  # makes this an async generator

    @base_hook
    async def accounts(self) -> AsyncIterator[Account]:
        """Yield the Accounts to bind Sessions to, once, before the Run starts.

        With a Session Pool, the pool then holds one Session per Account, under its
        id, and no other. Override it to import Accounts from elsewhere, such as a
        database; to add to those of the settings, yield those of
        `super().accounts()` too. Inline Fetches are not available yet. The values
        of the passwords and of the `extra` credentials are masked in the Run's log
        from then on. It is not called without a Session Pool.

        Yields:
            The Accounts of `sessions.accounts`, then those of
            `sessions.accounts_file`, unless overridden.
        """
        settings = self.crawler.settings.section(SessionSettings)
        for account in settings.accounts:
            yield account
        if settings.accounts_file is not None:
            for account in await asyncio.to_thread(read_accounts, settings.accounts_file):
                yield account

    @base_hook
    async def login(self, session: Session, account: Account | None) -> None:
        """Log a Session in, before it is first used, and again once it is logged out.

        Override it to have the Sessions of the Session Pool log in. It runs in the
        fetch that leases the Session first, or first after it was logged out, and
        no other fetch leases the Session meanwhile, as fetches waiting for it wait
        for the outcome. The Inline Fetches it makes, one after another, are sent
        under the Session, over HTTP, which keeps the cookies they set; their
        Responses are not judged with `is_logged_out()`. A Session without an
        Account logs in too, which can warm it up, such as to take a guest token.

        Check that the login worked, and raise if not: a login that raises is
        logged, and tried again once the Session cooled down for
        `sessions.cooldown` seconds, while the Requests waiting for it lease other
        Sessions; after `sessions.login_attempts` failures in a row the Session is
        Retired. A Session logged out by the very fetch it logged in for counts as a
        failure too.

        Args:
            session: The Session to log in; set its cookies yourself where a login
                hands out a token instead.
            account: The Account the Session is bound to, if any.

        Example:
            >>> async def login(self, session, account):
            ...     form = await self.fetch("https://shop.example/login")
            ...     token = form.css("input[name=csrf]::attr(value)").get()
            ...     data = {"user": account.username, "password": account.password.reveal(), "csrf": token}
            ...     page = await self.fetch(Request("https://shop.example/login", data=data))
            ...     if page.css(".error"):
            ...         raise RuntimeError(f"cannot log in as {account.id}")
        """

    def is_logged_out(self, response: Response) -> bool:
        """Tell whether a Response shows the Session it was fetched under logged out.

        Once `login()` is overridden, every Response fetched under a Session is
        judged, but those of a login, and a streamed one before its body is read. A
        Session found logged out logs in again before its next fetch, and the
        Request is retried at once, which counts as a retry. Override it for sites
        that show a login page instead, such as by redirecting to one.

        Args:
            response: The Response.

        Returns:
            Whether its status is 401, unless overridden.
        """
        return response.status == 401

    def parse(self, response: Response) -> CallbackResult:
        """Handle a Response whose Request names no Callback.

        Override it as an async generator that yields Requests and Items, or as a
        coroutine that returns one output or a list of them.

        Args:
            response: The Response to parse.

        Returns:
            The outputs, as an async iterator or an awaitable.

        Raises:
            NotImplementedError: The subclass did not implement it.
        """
        raise NotImplementedError(f"{type(self).__name__}.parse() is not implemented")

    async def fetch(self, request: Request | str) -> Response:
        """Fetch a Request in place and return its Response: an Inline Fetch.

        Use it to log in, to page through an API in order, or to assemble an Item
        from several pages in one Callback. The Request goes through the download
        Middleware and the rate limits, but not the Frontier: it is not deduplicated
        or stored, and its Callback never runs. Failed attempts are retried in place.
        It continues the Session of the Request being handled, if any; otherwise it
        leases one when a Session Pool is enabled, and without one it sends only the
        cookies it carries. See `Crawler.fetch()`.

        Args:
            request: The Request, or a URL to GET; a relative URL resolves against the
                Response whose Callback is running.

        Returns:
            The Response, whatever its status code, unless the retry policy retries it.

        Raises:
            RuntimeError: The Run is not running.
            pernix.errors.RetriesExhausted: The retry policy ran out of retries.
            pernix.errors.FetchError: The fetch failed in a way the policy does not
                retry.

        Example:
            >>> async def parse(self, response):
            ...     login = await self.fetch(Request("/login", data={"user": "me"}))  # a POST
            ...     account = await self.fetch(Request("/account", cookies=login.cookies))
            ...     yield {"name": account.css("h1::text").get()}
        """
        return await self.crawler.fetch(request)

    def stream(
        self, request: Request | str, *, max_bytes: int | None = None
    ) -> AbstractAsyncContextManager[StreamedResponse]:
        """Fetch a Request in place, reading its body as it arrives, such as to download a file.

        As `fetch()`, but the body is read once, with `iter_bytes()` or `save()`, and
        a slow reader keeps only about a megabyte of it in memory. See
        `Crawler.stream()`.

        Args:
            request: The Request, or a URL to GET.
            max_bytes: Bytes the body may have at most; `None` for the Fetcher's own
                limit, such as `http.max_body_size`, and 0 for no limit.

        Returns:
            An async context manager giving the `StreamedResponse`; the transfer is
            closed on exit.

        Raises:
            RuntimeError: The Run is not running.

        Example:
            >>> async def parse(self, response):
            ...     async with self.stream(response.css("a.video::attr(href)").get(), max_bytes=0) as video:
            ...         await video.save(f"videos/{response.meta['id']}.mp4")
        """
        return self.crawler.stream(request, max_bytes=max_bytes)

    @classmethod
    def run(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> RunResult:
        """Run this Spider to completion in a new event loop, as the main program.

        SIGINT or SIGTERM drains the Run, and a second one force-stops it. When the
        Run does not complete, the process exits: with 1 when it failed, with 2 for a
        configuration error, such as invalid settings or Spider arguments, and with
        130 when a signal stopped it. Use `crawl()` to handle errors in code.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            **args: The Spider arguments.

        Returns:
            How the Run ended, when it finished or code stopped it.

        Raises:
            SystemExit: The Run failed, was misconfigured, or was stopped by a signal.

        Example:
            >>> if __name__ == "__main__":
            ...     Books.run(output="books.jsonl", settings={"concurrency": 32})
        """
        return run_spider(cls, args=args, settings=settings, output=output)

    @classmethod
    async def crawl(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> RunResult:
        """Run this Spider inside an existing event loop.

        Signals are left to the application; cancelling the task stops the Run like
        a Force Stop, and raises `CancelledError`. For more control, such as calling
        `stop()`, create a `pernix.crawler.Crawler` with `Crawler.from_spider()`.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting, such as
                `"books.jsonl"`, `"books.csv"`, or `"books.json"`.
            **args: The Spider arguments.

        Returns:
            How the Run ended.

        Raises:
            pernix.errors.SettingsError: The settings are invalid; nothing has been
                fetched or written yet.
            pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.
        """
        return await Crawler.from_spider(cls, args=args, settings=settings, output=output).crawl()

    @classmethod
    def stream_items(
        cls,
        *,
        settings: Mapping[str, Any] | None = None,
        output: str | os.PathLike[str] | None = None,
        **args: Any,
    ) -> ItemStream:
        """Run this Spider inside an existing event loop, streaming the Items that pass every Pipeline.

        The Run starts with the first iteration. The stream holds one Item at a time,
        so a slow consumer slows the Run down. Leaving early force-stops the Run; an
        `async with` block waits until it has stopped. If the Run fails, the error is
        raised after the Items already received.

        Args:
            settings: Settings for this Run, by dotted key; they take precedence over
                every other layer.
            output: A Feed for the Items besides the `feeds` setting.
            **args: The Spider arguments.

        Returns:
            The Items, as an async iterator and an async context manager.

        Raises:
            pernix.errors.SettingsError: The settings are invalid.
            pernix.errors.SpiderArgumentError: The Spider arguments are invalid.
            pernix.errors.AddonError: An enabled Addon cannot be loaded.

        Example:
            >>> async with Books.stream_items(keyword="dune") as items:
            ...     async for item in items:
            ...         print(item["title"])
        """
        return Crawler.from_spider(cls, args=args, settings=settings, output=output).stream_items()
