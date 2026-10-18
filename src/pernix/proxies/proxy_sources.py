"""The sources of proxies: a list, a file, an API, or a provider of one's own."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pernix.errors import FetchError, SettingsError
from pernix.proxies.proxy_url import proxy_url
from pernix.request import Request

if TYPE_CHECKING:
    import os
    from collections.abc import Collection, Iterable

    from pernix.fetchers import HTTPFetcher
    from pernix.secret import Secret

__all__ = ["ProxyAPI", "ProxyFile", "ProxyProvider", "StaticProxies", "proxy_lines", "read_proxies"]


class ProxyProvider(Protocol):
    """Gives the proxies to send Requests through, such as from a database; set one as `proxies.provider`.

    `proxies()` is called when the Run starts, and again every `proxies.refresh`
    seconds, if set; the proxies it gives then replace those it gave before. They
    are URLs such as `http://user:password@proxy:8080`, or `proxy:8080` for HTTP;
    those that are not valid are skipped, with a warning. Like other components, a
    provider gets services injected, and is opened and closed with the Run.

    Example:
        >>> class FromDatabase:
        ...     def __init__(self, database: Database) -> None:
        ...         self.database = database
        ...
        ...     async def proxies(self) -> list[str]:
        ...         rows = await self.database.fetch("SELECT url FROM proxies WHERE enabled")
        ...         return [row["url"] for row in rows]
    """

    async def proxies(self) -> Iterable[str]:
        """Return the proxies to use now.

        Returns:
            The proxy URLs.
        """
        ...


class StaticProxies:
    """The proxies of `proxies.urls`, which never change."""

    def __init__(self, urls: Collection[str]) -> None:
        """Hold the proxies.

        Args:
            urls: The proxy URLs.
        """
        self._urls = tuple(urls)

    async def proxies(self) -> tuple[str, ...]:
        """Return the proxies."""
        return self._urls


class ProxyFile:
    """The proxies of a text file, one per line, read anew each time; see `read_proxies()`."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Name the file.

        Args:
            path: The file.
        """
        self._path = Path(path)

    async def proxies(self) -> list[str]:
        """Read the proxies of the file.

        Returns:
            The proxy URLs, in the order of the file.

        Raises:
            pernix.errors.SettingsError: The file cannot be read, or some of its
                lines are not proxies.
        """
        return await asyncio.to_thread(read_proxies, self._path)


class ProxyAPI:
    """The proxies an API answers with, one per line, such as a proxy vendor's, fetched anew each time.

    The API is fetched over HTTP with a fetcher of its own, opened and closed with
    the Run, and never through a proxy. Blank lines and those starting with `#` are
    skipped.
    """

    def __init__(self, url: Secret, fetcher: HTTPFetcher) -> None:
        """Name the API.

        Args:
            url: The API's URL, a `Secret` as it often holds a key.
            fetcher: The fetcher to fetch it with, not opened yet.
        """
        self._url = url
        self._fetcher = fetcher

    async def open(self) -> None:
        """Open the fetcher."""
        await self._fetcher.__aenter__()

    async def close(self) -> None:
        """Close the fetcher."""
        await self._fetcher.__aexit__(None, None, None)

    async def proxies(self) -> list[str]:
        """Fetch the proxies the API answers with.

        Returns:
            The lines of the answer that are not blank or comments, in order.

        Raises:
            pernix.errors.FetchError: The API could not be fetched, or answered with
                a status other than 2xx.
        """
        response = await self._fetcher.fetch(Request(self._url.reveal()))
        if not 200 <= response.status < 300:
            raise FetchError(f"the proxy API answered with the status {response.status}")
        return [proxy for _, proxy in proxy_lines(response.text)]


def proxy_lines(text: str) -> list[tuple[int, str]]:
    """Return the entries of a text of one proxy per line, skipping blank lines and those starting with `#`.

    Args:
        text: The text.

    Returns:
        Each entry, stripped, with its line number, counted from 1.
    """
    lines = ((number, line.strip()) for number, line in enumerate(text.splitlines(), start=1))
    return [(number, line) for number, line in lines if line and not line.startswith("#")]


def read_proxies(path: str | os.PathLike[str]) -> list[str]:
    """Read proxies from a text file, one per line, as `proxy_url()` takes them.

    Blank lines and those starting with `#` are skipped. No proxy is ever quoted in
    an error, as it may hold credentials.

    Args:
        path: The file.

    Returns:
        The proxy URLs, in the order of the file.

    Raises:
        pernix.errors.SettingsError: The file cannot be read, or some of its lines
            are not proxies, each of which it names.
    """
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SettingsError([f"{file}: cannot read the proxies: {exc.strerror or exc}"]) from None
    except UnicodeDecodeError:
        raise SettingsError([f"{file}: the proxies are not UTF-8 text"]) from None
    proxies: list[str] = []
    problems: list[str] = []
    for number, line in proxy_lines(text):
        try:
            proxies.append(proxy_url(line))
        except ValueError as exc:
            problems.append(f"{file}, line {number}: {exc}")
    if problems:
        raise SettingsError(problems)
    return proxies
