from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, overload

import orjson
from parsel import Selector, SelectorList

from pernix.headers import Headers, HeadersInput
from pernix.text_decoding import codec_for, decode_text
from pernix.urls import document_base, join_url

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from re import Pattern

    from pernix.request import Request
    from pernix.text_decoding import DecodedText, EncodingSource

__all__ = ["Redirect", "Response", "expect_response"]

_UNSET: Any = object()


@dataclass(frozen=True, slots=True)
class Redirect:
    """One redirect response on the way to the final Response.

    Attributes:
        url: The URL that answered with the redirect.
        status: The redirect status code, such as 301 or 302.
        headers: The headers of the redirect response, including `Location`.
    """

    url: str
    status: int
    headers: Headers


@dataclass(frozen=True, slots=True, init=False, eq=False, repr=False)
class Response:
    """The immutable result of one fetch, with lazily decoded text and selectors.

    The text is decoded on first use; `encoding` and `encoding_source` tell how the
    encoding was chosen (see `pernix.text_decoding`). Derive a copy with another
    encoding, body, or other changes with `replace()`.

    Attributes:
        url: The final URL, after redirects.
        status: The HTTP status code.
        headers: The response headers, in wire order.
        body: The raw response body.
        request: The Request that produced this Response.
        cookies: Read-only cookies the client holds after the fetch, by name: those the
            Request carried (`cookies=` or a `Cookie` header) plus those set during the
            fetch, including on redirects.
            Pass them on with `Request(url, cookies=response.cookies)`.
        history: The redirects followed to reach `url`, oldest first.
        elapsed: Seconds the fetch took, if known.
        ip_address: The IP address of the server, if known.
        http_version: The HTTP version used, such as `"HTTP/2"`, if known.
        fetcher: The name of the Fetcher that produced this Response, such as `"http"`.
        session: The Session the Request was sent as; `None` for none.
        streamed: Whether the body is read as it arrives, apart from the Response,
            whose `body` is then empty: download Middleware see such Responses for
            the streamed fetches of `Crawler.stream()`.
    """

    url: str
    status: int
    headers: Headers
    body: bytes
    request: Request
    cookies: Mapping[str, str]
    history: tuple[Redirect, ...]
    elapsed: float | None
    ip_address: str | None
    http_version: str | None
    fetcher: str | None
    session: str | None
    streamed: bool
    _encoding: str | None
    _default_encoding: str
    _decoded: DecodedText | None
    _selector: Selector | None
    _json: Any
    _base_url: str | None

    def __init__(
        self,
        *,
        url: str,
        status: int,
        headers: HeadersInput,
        body: bytes,
        request: Request,
        cookies: Mapping[str, str] | None = None,
        encoding: str | None = None,
        default_encoding: str = "utf-8",
        history: Sequence[Redirect] = (),
        elapsed: float | None = None,
        ip_address: str | None = None,
        http_version: str | None = None,
        fetcher: str | None = None,
        session: str | None = None,
        streamed: bool = False,
    ) -> None:
        """Create a Response. Fetchers build these; user code rarely needs to.

        Args:
            url: The final URL, after redirects.
            status: The HTTP status code.
            headers: The response headers.
            body: The raw response body.
            request: The Request that produced this Response.
            cookies: The cookies the client holds after the fetch, by name.
            encoding: Decode the body with this encoding, whatever it declares.
            default_encoding: The encoding used when nothing declares one, the body is
                not UTF-8, and detection fails.
            history: The redirects followed, oldest first.
            elapsed: Seconds the fetch took.
            ip_address: The IP address of the server.
            http_version: The HTTP version used.
            fetcher: The name of the Fetcher that produced this Response.
            session: The Session the Request was sent as.
            streamed: Whether the body is read apart from the Response.

        Raises:
            LookupError: `encoding` or `default_encoding` names no text encoding.
        """
        for label in (encoding, default_encoding):
            if label is not None:
                codec_for(label)
        set_field = object.__setattr__
        set_field(self, "url", url)
        set_field(self, "status", status)
        set_field(self, "headers", Headers(headers))
        set_field(self, "body", body)
        set_field(self, "request", request)
        set_field(self, "cookies", MappingProxyType(dict(cookies or {})))
        set_field(self, "history", tuple(history))
        set_field(self, "elapsed", elapsed)
        set_field(self, "ip_address", ip_address)
        set_field(self, "http_version", http_version)
        set_field(self, "fetcher", fetcher)
        set_field(self, "session", session)
        set_field(self, "streamed", streamed)
        set_field(self, "_encoding", encoding)
        set_field(self, "_default_encoding", default_encoding)
        set_field(self, "_decoded", None)
        set_field(self, "_selector", None)
        set_field(self, "_json", _UNSET)
        set_field(self, "_base_url", None)

    def replace(self, **changes: Any) -> Response:
        """Return a copy of this Response with some constructor arguments replaced.

        Args:
            **changes: New values, keyed by constructor argument. Pass `encoding` to
                decode the same body with another encoding.

        Returns:
            The new Response; this one is unchanged.

        Raises:
            TypeError: A key is not a constructor argument of Response.
        """
        values = self._arguments()
        unknown = sorted(set(changes) - set(values))
        if unknown:
            raise TypeError(f"replace() got unknown arguments {unknown}; Response takes {list(values)}")
        return Response(**(values | changes))

    @property
    def meta(self) -> Mapping[str, Any]:
        """The read-only user data of the originating Request."""
        return self.request.meta

    @property
    def text(self) -> str:
        """The body decoded as `encoding`, with undecodable bytes replaced."""
        return self._decode().text

    @property
    def encoding(self) -> str:
        """The Python codec that decodes the body, such as `"gb18030"`."""
        return self._decode().encoding

    @property
    def encoding_source(self) -> EncodingSource:
        """How `encoding` was chosen, such as `"header"` or `"detected"`."""
        return self._decode().source

    @property
    def selector(self) -> Selector:
        """A selector over the document, built on first use.

        XML media types are parsed as XML and JSON media types as JSON; anything else
        is JSON if it is a JSON object or array, XML if it starts with an XML
        declaration, and HTML otherwise.
        """
        selector = self._selector
        if selector is None:
            selector = self._build_selector()
            object.__setattr__(self, "_selector", selector)
        return selector

    def css(self, query: str) -> SelectorList[Selector]:
        """Select elements with a CSS query.

        Args:
            query: A CSS selector, optionally with `::text` or `::attr(name)`.

        Returns:
            The matching selectors.
        """
        return self.selector.css(query)

    def xpath(self, query: str, **kwargs: Any) -> SelectorList[Selector]:
        """Select elements with an XPath query.

        Args:
            query: An XPath expression.
            **kwargs: Values for XPath variables used in `query`.

        Returns:
            The matching selectors.
        """
        return self.selector.xpath(query, **kwargs)

    def jmespath(self, query: str, **kwargs: Any) -> SelectorList[Selector]:
        """Select values from a JSON document with a JMESPath query.

        `get()` and `getall()` on the result return the JSON values as they are, so
        numbers stay numbers. For JSON embedded in HTML, query a selection instead,
        such as `response.css("script#data::text").jmespath("props.title")`.

        Args:
            query: A JMESPath expression.
            **kwargs: Options passed on to `jmespath.search()`.

        Returns:
            The matching values; empty if the document is not JSON.
        """
        return self.selector.jmespath(query, **kwargs)

    def re(self, regex: str | Pattern[str], *, replace_entities: bool = True) -> list[str]:
        """Find every match of a regular expression in the decoded text.

        A named group `extract`, or otherwise every group, gives the strings
        returned, as with `css(...).re()`.

        Args:
            regex: The regular expression.
            replace_entities: Replace character entity references, except `&amp;`
                and `&lt;`, in the results.

        Returns:
            The matched strings.
        """
        return Selector(text=self.text, type="text").re(regex, replace_entities=replace_entities)

    @overload
    def re_first(
        self, regex: str | Pattern[str], default: None = None, *, replace_entities: bool = True
    ) -> str | None: ...

    @overload
    def re_first(self, regex: str | Pattern[str], default: str, *, replace_entities: bool = True) -> str: ...

    def re_first(
        self, regex: str | Pattern[str], default: str | None = None, *, replace_entities: bool = True
    ) -> str | None:
        """Return the first string `re()` would return.

        Args:
            regex: The regular expression.
            default: The value returned when nothing matches.
            replace_entities: Replace character entity references, as `re()` does.

        Returns:
            The first matched string, or `default`.
        """
        return next(iter(self.re(regex, replace_entities=replace_entities)), default)

    def json(self) -> Any:
        """Decode the text as JSON, once; later calls return the same object.

        Returns:
            The decoded value.

        Raises:
            ValueError: The text is not valid JSON.
        """
        if self._json is _UNSET:
            object.__setattr__(self, "_json", orjson.loads(self.text))
        return self._json

    def urljoin(self, url: str) -> str:
        """Resolve a URL against this Response, as a browser would resolve a link in it.

        The base is the final URL, or the first `<base href>` of an HTML document
        unless browsers would ignore it, as they do `data:` and `javascript:` bases.
        Requests constructed in a Callback do this to relative URLs automatically;
        use this method for links stored in Items.

        Args:
            url: An absolute or relative URL.

        Returns:
            The absolute URL.
        """
        base = self._base_url
        if base is None:
            href = self.selector.xpath("(//base[@href])[1]/@href").get() if self.selector.type == "html" else None
            base = document_base(self.url, href)
            object.__setattr__(self, "_base_url", base)
        return join_url(base, url)

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"

    def _decode(self) -> DecodedText:
        decoded = self._decoded
        if decoded is None:
            decoded = decode_text(
                self.body,
                content_type=self._content_type(),
                encoding=self._encoding,
                default_encoding=self._default_encoding,
            )
            object.__setattr__(self, "_decoded", decoded)
        return decoded

    def _content_type(self) -> str | None:
        """The last `Content-Type` header, which is the one that applies when several are sent."""
        values = self.headers.get_all("Content-Type")
        return values[-1] if values else None

    def _build_selector(self) -> Selector:
        media_type = (self._content_type() or "").partition(";")[0].strip().lower()
        subtype = media_type.partition("/")[2]
        if subtype == "json" or subtype.endswith("+json"):
            try:
                return Selector(root=self.json(), type="json")
            except ValueError:
                pass  # mislabelled; let the content decide
        is_xml = (subtype == "xml" or subtype.endswith("+xml")) and subtype != "xhtml+xml"
        return Selector(text=self.text, type="xml" if is_xml else None, base_url=self.url)

    def _arguments(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
            "request": self.request,
            "cookies": self.cookies,
            "encoding": self._encoding,
            "default_encoding": self._default_encoding,
            "history": self.history,
            "elapsed": self.elapsed,
            "ip_address": self.ip_address,
            "http_version": self.http_version,
            "fetcher": self.fetcher,
            "session": self.session,
            "streamed": self.streamed,
        }


def expect_response(value: object, source: str) -> Response:
    """Check that a component returned a Response, as components may break their contract.

    Args:
        value: What the component returned.
        source: Names the component in the error, such as `"Fetcher 'http'"`.

    Returns:
        `value`, as a Response.

    Raises:
        TypeError: `value` is not a Response.
    """
    if not isinstance(value, Response):
        raise TypeError(f"{source} returned {value!r}, which is not a Response")
    return value
