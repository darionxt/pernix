from __future__ import annotations

from typing import ClassVar

import pytest

from pernix import Request, Response
from pernix.errors import ComponentError, SettingsError
from pernix.fetchers import FetcherDispatch


class FakeFetcher:
    schemes: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, label: str = "fake") -> None:
        self.label = label
        self.requests: list[Request] = []

    async def fetch(self, request: Request) -> Response:
        self.requests.append(request)
        return Response(url=request.url, status=200, headers=[], body=self.label.encode(), request=request, fetcher="x")


class Web(FakeFetcher):
    schemes = frozenset({"http", "https"})


class Files(FakeFetcher):
    schemes = frozenset({"ftp", "sftp"})


async def test_the_url_scheme_selects_the_fetcher() -> None:
    dispatch = FetcherDispatch({"web": Web("web"), "files": Files("files")})

    web = await dispatch(Request("https://example.com/"))
    files = await dispatch(Request("FTP://files.example.com/a.txt"))

    assert (web.body, web.fetcher) == (b"web", "web")
    assert (files.body, files.fetcher) == (b"files", "files")


async def test_an_explicit_fetcher_wins_over_render_and_the_scheme() -> None:
    dispatch = FetcherDispatch({"web": Web("web"), "browser": FakeFetcher("browser"), "s3": FakeFetcher("s3")})

    explicit = await dispatch(Request("https://example.com/", fetcher="s3", render=True))
    rendered = await dispatch(Request("https://example.com/", render=True))

    assert (explicit.body, explicit.fetcher) == (b"s3", "s3")
    assert (rendered.body, rendered.fetcher) == (b"browser", "browser")


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (Request("https://example.com/", fetcher="s3"), "names the Fetcher 's3', which is not registered"),
        (Request("https://example.com/", render=True), "needs rendering, but the 'browser' Fetcher is not registered"),
        (Request("ftp://example.com/a"), "no registered Fetcher fetches 'ftp' URLs"),
    ],
)
async def test_requests_no_fetcher_fits_fail(request_: Request, message: str) -> None:
    with pytest.raises(LookupError, match=message):
        await FetcherDispatch({"web": Web()})(request_)


def test_two_fetchers_may_not_share_a_scheme() -> None:
    with pytest.raises(SettingsError) as excinfo:
        FetcherDispatch({"web": Web(), "proxy": Web(), "files": Files()})

    assert excinfo.value.problems == [
        "fetchers: 'web' and 'proxy' both fetch 'http' URLs; register a replacement under the name 'web', "
        "or disable one of them",
        "fetchers: 'web' and 'proxy' both fetch 'https' URLs; register a replacement under the name 'web', "
        "or disable one of them",
    ]


def test_fetchers_need_an_async_fetch_method() -> None:
    class Blocking:
        def fetch(self, request: Request) -> Response:
            raise NotImplementedError

    with pytest.raises(ComponentError, match=r"the Fetcher 'sync', .*, has no 'async def fetch\(self, request\)'"):
        FetcherDispatch({"sync": Blocking()})


async def test_fetchers_must_return_responses() -> None:
    class Broken(Web):
        async def fetch(self, request: Request) -> Response:
            return b"body"  # type: ignore[return-value]

    with pytest.raises(TypeError, match="the Fetcher 'web' returned b'body', which is not a Response"):
        await FetcherDispatch({"web": Broken()})(Request("https://example.com/"))
