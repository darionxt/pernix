from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from curl_cffi import CurlECode, CurlHttpVersion
from curl_cffi.requests import AsyncSession
from curl_cffi.requests import exceptions as curl_errors

from pernix.cookies import merge_set_cookies, parse_cookie_header
from pernix.errors import ConnectError, DecodeError, FetchError, ProxyError, Timeout, TLSError, TooManyRedirects
from pernix.headers import Headers
from pernix.response import Redirect, Response
from pernix.settings import CoreSettings, ResponseSettings

if TYPE_CHECKING:
    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

    from pernix.crawler import Crawler
    from pernix.request import Request

__all__ = ["HTTPFetcher"]

_HTTP_VERSIONS: dict[int, str] = {
    CurlHttpVersion.V1_0: "HTTP/1.0",
    CurlHttpVersion.V1_1: "HTTP/1.1",
    CurlHttpVersion.V2_0: "HTTP/2",
    CurlHttpVersion.V3: "HTTP/3",
}
"""What libcurl reports as the HTTP version of a transfer, by name."""

_ERROR_KINDS: tuple[tuple[type[curl_errors.RequestException], type[FetchError]], ...] = (
    (curl_errors.ProxyError, ProxyError),
    (curl_errors.SSLError, TLSError),  # before ConnectionError, its base
    (curl_errors.Timeout, Timeout),  # before ConnectionError, a base of ConnectTimeout
    (curl_errors.ConnectionError, ConnectError),
    (curl_errors.IncompleteRead, ConnectError),
    (curl_errors.TooManyRedirects, TooManyRedirects),
)
"""curl-cffi errors and the FetchErrors they become, most specific first."""

_ERROR_CODES: dict[int, type[FetchError]] = {CurlECode.BAD_CONTENT_ENCODING: DecodeError}
"""libcurl error codes that curl-cffi raises with a generic class, and the FetchErrors they become."""


class HTTPFetcher:
    """Fetches Requests over HTTP with curl-cffi.

    Use it as an async context manager: the underlying session is created on entry,
    bound to the running event loop, and closed on exit.

    Requests are independent (ADR 0011): each transfer sends only `request.cookies`,
    cookies set during it flow within its own redirect chain, and none are kept for
    later Requests. Connections are still reused.

    Attributes:
        schemes: The URL schemes it fetches by default: `http` and `https`.
    """

    schemes: ClassVar[frozenset[str]] = frozenset({"http", "https"})

    def __init__(self, *, max_clients: int = 16, default_encoding: str = "utf-8") -> None:
        """Configure the fetcher.

        Args:
            max_clients: The maximum number of requests in flight at once.
            default_encoding: The `default_encoding` of the Responses.
        """
        self._max_clients = max_clients
        self._default_encoding = default_encoding
        self._session: AsyncSession | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Build the fetcher for a Run from its settings.

        Args:
            crawler: The Crawler of the Run.

        Returns:
            A fetcher allowing `concurrency` requests in flight, whose Responses use
            `response.default_encoding`.
        """
        return cls(
            max_clients=crawler.settings.section(CoreSettings).concurrency,
            default_encoding=crawler.settings.section(ResponseSettings).default_encoding,
        )

    async def __aenter__(self) -> Self:
        self._session = AsyncSession(max_clients=self._max_clients, trust_env=False)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, request: Request) -> Response:
        """Send a Request and read the whole response body.

        Args:
            request: The Request to send.

        Returns:
            The Response, whatever its status code.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            pernix.errors.FetchError: The transfer failed; a subclass names the kind of
                failure, and `code` is libcurl's error code.
        """
        if self._session is None:
            raise RuntimeError("HTTPFetcher must be entered with 'async with' before fetching")
        options: dict[str, Any] = {}
        if request.timeout is not None:
            options["timeout"] = request.timeout
        try:
            result = await self._session.request(
                cast("HttpMethod", request.method),
                request.url,
                headers=request.headers.items(),
                data=request.body,
                cookies=dict(request.cookies),
                discard_cookies=True,
                proxy=request.proxy,
                impersonate=cast("Any", request.impersonate),
                **options,
            )
        except curl_errors.RequestException as exc:
            raise _fetch_error(exc) from exc
        hops = [*result.history, result]
        set_cookies = [value for hop in hops for value in hop.headers.get_list("set-cookie") if value is not None]
        return Response(
            url=result.url,
            status=result.status_code,
            headers=_headers(result),
            body=result.content,
            request=request,
            cookies=merge_set_cookies(_sent_cookies(request), set_cookies),
            default_encoding=self._default_encoding,
            history=[Redirect(hop.url, hop.status_code, _headers(hop)) for hop in result.history],
            elapsed=result.elapsed.total_seconds(),
            ip_address=result.primary_ip or None,
            http_version=_HTTP_VERSIONS.get(result.http_version),
            fetcher="http",
        )


def _fetch_error(error: curl_errors.RequestException) -> FetchError:
    """Map a curl-cffi error onto the FetchError of its kind, keeping libcurl's code."""
    code = int(error.code)
    kind = _ERROR_CODES.get(code) or next(
        (kind for curl_kind, kind in _ERROR_KINDS if isinstance(error, curl_kind)), FetchError
    )
    return kind(str(error), code=code)


def _headers(response: curl_cffi.Response) -> Headers:
    return Headers((name, value) for name, value in response.headers.multi_items() if value is not None)


def _sent_cookies(request: Request) -> dict[str, str]:
    """The cookies a Request carries, whether as `cookies=` or as `Cookie` headers."""
    sent: dict[str, str] = {}
    for header in request.headers.get_all("Cookie"):
        sent.update(parse_cookie_header(header))
    sent.update(request.cookies)
    return sent
