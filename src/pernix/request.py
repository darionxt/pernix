from __future__ import annotations

import dataclasses
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlencode

import orjson

from pernix.headers import Headers, HeadersInput
from pernix.urls import clean_url, is_absolute

__all__ = ["Callback", "CallbackResult", "FormData", "Request", "resolve_relative_urls"]

type CallbackResult = AsyncIterator[Any] | Awaitable[Any]
"""What a Callback returns: an async generator of outputs, or an awaitable of one output or a list."""

type Callback = Callable[..., CallbackResult]
"""A Spider method that receives a Response, plus the Request's `kwargs`, and produces Requests and Items."""

type FormData = Mapping[str, str | Sequence[str]] | Sequence[tuple[str, str]]
"""Form fields for `Request(data=...)`, as a mapping or as `(name, value)` pairs."""

_JSON_CONTENT_TYPE = "application/json"
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

_url_resolver: ContextVar[Callable[[str], str] | None] = ContextVar("pernix_url_resolver", default=None)


@contextmanager
def resolve_relative_urls(urljoin: Callable[[str], str]) -> Generator[None]:
    """Resolve relative URLs of the Requests constructed inside the block with `urljoin`.

    The Engine wraps each step of a Callback in this, passing the `urljoin` of the
    Response being handled.

    Args:
        urljoin: Turns a relative URL into an absolute one.

    Yields:
        Nothing; the block runs with the resolver in effect.
    """
    token = _url_resolver.set(urljoin)
    try:
        yield
    finally:
        _url_resolver.reset(token)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Request:
    """An immutable description of one fetch and the Callback that receives its Response.

    Requests are independent by default: a Request sends only the cookies it carries,
    and no cookies persist from one Request to another (see ADR 0011). Derive
    modified copies with `replace()`.

    A relative URL is resolved when the Request is constructed, against the Response
    whose Callback is running (its final URL, or its `<base href>`), so `url` is
    always absolute. Outside a Callback, such as in `start()`, a relative URL is an
    error.

    Attributes:
        url: The absolute URL to fetch.
        method: The HTTP method, upper-cased.
        headers: The request headers, in wire order.
        body: The request body, if any.
        callback: The Spider method that receives the Response; `None` means `parse`.
        errback: The Spider method called if the Request fails.
        kwargs: Read-only keyword arguments passed to the Callback.
        meta: Read-only user data carried to the Response.
        priority: Higher priorities are fetched first.
        dont_filter: Skip deduplication.
        fingerprint: An application-supplied Fingerprint; computed when `None`.
        cookies: Read-only cookies sent with this Request only, by name.
        session: The Session to send the Request as; `None` for none.
        render: Whether to load the page in the browser.
        fetcher: The name of the Fetcher to use; inferred when `None`.
        timeout: Seconds before the fetch fails; the setting applies when `None`.
        proxy: A proxy URL for this Request only.
        impersonate: A Profile override for this Request only.
        ext: Read-only component-private data, by component name.
    """

    url: str
    method: str
    headers: Headers
    body: bytes | None
    callback: Callback | None
    errback: Callback | None
    kwargs: Mapping[str, Any]
    meta: Mapping[str, Any]
    priority: int
    dont_filter: bool
    fingerprint: str | None
    cookies: Mapping[str, str]
    session: str | None
    render: bool
    fetcher: str | None
    timeout: float | None
    proxy: str | None
    impersonate: str | None
    ext: Mapping[str, Mapping[str, Any]]

    def __init__(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: HeadersInput = None,
        body: bytes | str | None = None,
        json: Any = None,
        data: FormData | None = None,
        callback: Callback | None = None,
        errback: Callback | None = None,
        kwargs: Mapping[str, Any] | None = None,
        meta: Mapping[str, Any] | None = None,
        priority: int = 0,
        dont_filter: bool = False,
        fingerprint: str | None = None,
        cookies: Mapping[str, str] | None = None,
        session: str | None = None,
        render: bool = False,
        fetcher: str | None = None,
        timeout: float | None = None,
        proxy: str | None = None,
        impersonate: str | None = None,
        ext: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        """Create a Request.

        Args:
            url: The URL to fetch. Inside a Callback it may be relative to the
                Response being handled. Surrounding whitespace, tabs, and newlines are
                removed, as browsers do.
            method: The HTTP method; any case is accepted.
            headers: Request headers as a mapping or as `(name, value)` pairs.
            body: The raw request body; a `str` is encoded as UTF-8.
            json: A value sent as a JSON body, with `Content-Type: application/json`
                unless the headers set one.
            data: Form fields sent URL-encoded, with
                `Content-Type: application/x-www-form-urlencoded` unless the headers
                set one.
            callback: A Spider method that receives the Response. Defaults to `parse`.
            errback: A Spider method called if the Request fails.
            kwargs: Keyword arguments passed to the Callback.
            meta: User data carried to the Response.
            priority: Higher priorities are fetched first.
            dont_filter: Skip deduplication.
            fingerprint: An application-supplied Fingerprint.
            cookies: Cookies sent with this Request only, by name.
            session: The Session to send the Request as.
            render: Load the page in the browser.
            fetcher: The name of the Fetcher to use.
            timeout: Seconds before the fetch fails.
            proxy: A proxy URL for this Request only.
            impersonate: A Profile override for this Request only.
            ext: Component-private data, by component name.

        Raises:
            ValueError: `url` is relative outside a Callback; more than one of `body`,
                `json`, and `data` is given; `cookies` is combined with a `Cookie`
                header; or `timeout` is not positive.
            TypeError: `callback` or `errback` is not a method of an object.
        """
        url = _absolute(url)
        if sum(value is not None for value in (body, json, data)) > 1:
            raise ValueError("body, json, and data are mutually exclusive")
        request_headers = Headers(headers)
        if cookies and "Cookie" in request_headers:
            raise ValueError("pass cookies either as cookies= or as a Cookie header, not both")
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        for name, method_ in (("callback", callback), ("errback", errback)):
            if method_ is not None and not inspect.ismethod(method_):
                raise TypeError(f"{name} must be a method of the Spider, such as self.parse_item; got {method_!r}")

        if json is not None:
            body = orjson.dumps(json)
            request_headers = _with_default(request_headers, "Content-Type", _JSON_CONTENT_TYPE)
        elif data is not None:
            body = urlencode(data, doseq=True)
            request_headers = _with_default(request_headers, "Content-Type", _FORM_CONTENT_TYPE)

        set_field = object.__setattr__
        set_field(self, "url", url)
        set_field(self, "method", method.upper())
        set_field(self, "headers", request_headers)
        set_field(self, "body", body.encode() if isinstance(body, str) else body)
        set_field(self, "callback", callback)
        set_field(self, "errback", errback)
        set_field(self, "kwargs", _frozen(kwargs))
        set_field(self, "meta", _frozen(meta))
        set_field(self, "priority", priority)
        set_field(self, "dont_filter", dont_filter)
        set_field(self, "fingerprint", fingerprint)
        set_field(self, "cookies", _frozen(cookies))
        set_field(self, "session", session)
        set_field(self, "render", render)
        set_field(self, "fetcher", fetcher)
        set_field(self, "timeout", timeout)
        set_field(self, "proxy", proxy)
        set_field(self, "impersonate", impersonate)
        set_field(self, "ext", MappingProxyType({name: _frozen(values) for name, values in (ext or {}).items()}))

    def replace(self, **changes: Any) -> Request:
        """Return a copy of this Request with some fields replaced.

        Args:
            **changes: New values, keyed by field name. To change the body, pass
                `body`; `json` and `data` only exist when constructing a Request.

        Returns:
            The new Request; this one is unchanged.

        Raises:
            TypeError: A key is not a field of Request.
        """
        names = [field.name for field in dataclasses.fields(self)]
        unknown = sorted(set(changes) - set(names))
        if unknown:
            raise TypeError(f"replace() got unknown fields {unknown}; Request fields are {names}")
        values: dict[str, Any] = {name: getattr(self, name) for name in names}
        values.update(changes)
        return Request(**values)

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.url}>"


def _absolute(url: str) -> str:
    url = clean_url(url)
    if is_absolute(url):
        return url
    resolve = _url_resolver.get()
    if resolve is None:
        raise ValueError(
            f"{url!r} is a relative URL; Requests constructed outside a Callback, such as in start(), "
            "need an absolute URL"
        )
    resolved = resolve(url)
    if not is_absolute(resolved):  # the Response URL has a scheme relative URLs cannot be resolved against
        raise ValueError(f"the relative URL {url!r} cannot be resolved against the Response being handled")
    return resolved


def _frozen[V](values: Mapping[str, V] | None) -> Mapping[str, V]:
    return MappingProxyType(dict(values or {}))


def _with_default(headers: Headers, name: str, value: str) -> Headers:
    return headers if name in headers else Headers([*headers.items(), (name, value)])
