"""The sources of proxies: a list, a file, an API, or a provider of one's own."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import msgspec

from pernix.errors import FetchError, SettingsError
from pernix.proxies.json_answers import read_json_answer
from pernix.proxies.proxy import Proxy
from pernix.proxies.proxy_url import proxy_url
from pernix.request import Request

if TYPE_CHECKING:
    import os
    from collections.abc import Callable, Collection, Iterable

    from pernix.fetchers import HTTPFetcher
    from pernix.proxies.json_answers import JsonAnswer
    from pernix.secret import Secret

__all__ = ["ProxyAPI", "ProxyFile", "ProxyProvider", "StaticProxies", "proxy_lines", "read_proxies"]

logger = logging.getLogger(__name__)


class ProxyProvider(Protocol):
    """Gives the proxies to send Requests through, such as from a database; set one as `proxies.provider`.

    `proxies()` is called when the Run starts, and again every `proxies.refresh`
    seconds, if set; the proxies it gives then replace those it gave before, or,
    with `proxies.supply = "add"`, are added to them, and it is called on demand
    too, as the proxies run low. A provider whose `proxies()` takes a `wanted`
    keyword is told how many proxies are wanted, when that is known, such as to
    extract that many from an API. They are URLs such as
    `http://user:password@proxy:8080`, or `proxy:8080` for HTTP, or
    `pernix.proxies.Proxy` records, to tag them or tell when they expire; those
    that are not valid are skipped, with a warning. Like other components, a
    provider gets services injected, and is opened and closed with the Run.

    Example:
        >>> class FromDatabase:
        ...     def __init__(self, database: Database) -> None:
        ...         self.database = database
        ...
        ...     async def proxies(self) -> list[Proxy]:
        ...         rows = await self.database.fetch("SELECT url, region FROM proxies WHERE enabled")
        ...         return [Proxy(row["url"], tags=frozenset({row["region"]})) for row in rows]
    """

    async def proxies(self) -> Iterable[str | Proxy]:
        """Return the proxies to use now.

        Returns:
            The proxy URLs, or records of them.
        """
        ...


class StaticProxies:
    """The proxies of `proxies.urls`, which never change."""

    def __init__(self, urls: Collection[str | Proxy]) -> None:
        """Hold the proxies.

        Args:
            urls: The proxy URLs, or records of them.
        """
        self._urls = tuple(urls)

    async def proxies(self) -> tuple[str | Proxy, ...]:
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
    """The proxies an API answers with, one per line, or in JSON, such as a proxy vendor's, fetched anew each time.

    The API is fetched over HTTP with a fetcher of its own, opened and closed with
    the Run, and never through a proxy. `{count}` in its URL is replaced by how many
    proxies are wanted, or 1 when that is not known. An answer of one per line skips
    blank lines and those starting with `#`. A JSON answer is read as its
    `JsonAnswer` says; one that does not match it gives no proxy, with a warning
    naming the field it lacks, never the API's URL. With a lifetime, each proxy whose
    answer tells no expiry expires that long after the API answered.
    """

    def __init__(
        self,
        url: Secret,
        fetcher: HTTPFetcher,
        *,
        lifetime: float | None = None,
        answer: JsonAnswer | None = None,
        mask: Callable[[Iterable[str]], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Name the API.

        Args:
            url: The API's URL, a `Secret` as it often holds a key.
            fetcher: The fetcher to fetch it with, not opened yet.
            lifetime: Seconds each proxy it gives works for, unless its answer tells
                when it expires; `None` for no expiry.
            answer: How to read its answers, in JSON; `None` for one proxy per line.
            mask: Masks values in the Run's log from now on, given each URL fetched,
                whose count the `Secret` does not hold.
            clock: The current time, in seconds since the epoch.
        """
        self._url = url
        self._fetcher = fetcher
        self._lifetime = lifetime
        self._answer = answer
        self._mask = mask
        self._clock = clock

    async def open(self) -> None:
        """Open the fetcher."""
        await self._fetcher.__aenter__()

    async def close(self) -> None:
        """Close the fetcher."""
        await self._fetcher.__aexit__(None, None, None)

    async def proxies(self, wanted: int | None = None) -> list[str | Proxy]:
        """Fetch the proxies the API answers with.

        Args:
            wanted: How many proxies are wanted, for `{count}` in the URL; 1 when
                `None`.

        Returns:
            The proxies of the answer, in order: the lines that are not blank or
            comments, or the entries of a JSON answer, as records expiring when the
            answer tells, or after the lifetime, if set.

        Raises:
            pernix.errors.FetchError: The API could not be fetched, or answered with
                a status other than 2xx.
        """
        url = self._url.reveal().replace("{count}", str(wanted or 1))
        if self._mask is not None:
            self._mask([url])
        response = await self._fetcher.fetch(Request(url))
        if not 200 <= response.status < 300:
            raise FetchError(f"the proxy API answered with the status {response.status}")
        arrived = self._clock()
        if self._answer is not None:
            return self._read_json(response.text, self._answer, arrived)
        lines = [proxy for _, proxy in proxy_lines(response.text)]
        if self._lifetime is None:
            return list(lines)
        expires = arrived + self._lifetime
        return [Proxy(line, expires=expires) for line in lines]

    def _read_json(self, text: str, answer: JsonAnswer, arrived: float) -> list[str | Proxy]:
        """The proxies of a JSON answer, those without an expiry expiring after the lifetime, if set."""
        proxies, problems = read_json_answer(text, answer, now=arrived)
        if not proxies and problems:
            logger.warning("The answer of the proxy API gives no proxy: %s", "; ".join(problems))
        elif problems:
            logger.warning("Skipped the entries of the proxy API's answer that cannot be read: %s", ", ".join(problems))
        if self._lifetime is None:
            return list(proxies)
        lifetime = arrived + self._lifetime
        return [
            proxy if proxy.expires is not None else msgspec.structs.replace(proxy, expires=lifetime)
            for proxy in proxies
        ]


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
