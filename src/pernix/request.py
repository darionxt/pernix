from __future__ import annotations

import dataclasses
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlencode

import orjson

from pernix.browser.render import Render
from pernix.cookies import check_cookies
from pernix.headers import Headers, HeadersInput
from pernix.request_kinds import check_kind
from pernix.urls import clean_url, is_absolute

__all__ = ["Callback", "CallbackResult", "FormData", "QueryParams", "QueryValue", "Request", "resolve_relative_urls"]

type CallbackResult = AsyncIterator[Any] | Awaitable[Any]
"""What a Callback returns: an async generator of outputs, or an awaitable of one output or a list."""

type Callback = Callable[..., CallbackResult]
"""A Spider method that receives a Response, plus the Request's `kwargs`, and produces Requests and Items."""

type FormData = Mapping[str, str | Sequence[str]] | Sequence[tuple[str, str]]
"""Form fields for `Request(data=...)`, as a mapping or as `(name, value)` pairs."""

type QueryValue = str | int | float
"""The value of a query parameter; booleans are refused, as sites spell them differently."""

type QueryParams = Mapping[str, QueryValue | Sequence[QueryValue]] | Sequence[tuple[str, QueryValue]]
"""Query parameters for `Request(params=...)`, as a mapping or as `(name, value)` pairs; a sequence repeats a name."""

_JSON_CONTENT_TYPE = "application/json"
_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

_url_resolver: ContextVar[Callable[[str], str] | None] = ContextVar("pernix_url_resolver", default=None)

_referring_page: ContextVar[str | None] = ContextVar("pernix_referring_page", default=None)
"""The page the Requests constructed in a Callback come from, with `http.inherit_referer`."""


@contextmanager
def resolve_relative_urls(urljoin: Callable[[str], str], *, referer: str | None = None) -> Generator[None]:
    """Resolve relative URLs of the Requests constructed inside the block with `urljoin`.

    The Engine wraps each step of a Callback in this, passing the `urljoin` of the
    Response being handled, and, with `http.inherit_referer`, the page it belongs
    to.

    Args:
        urljoin: Turns a relative URL into an absolute one.
        referer: The page the Requests come from, which those without a `Referer`
            header take as theirs; `None` for none.

    Yields:
        Nothing; the block runs with the resolver in effect.
    """
    token = _url_resolver.set(urljoin)
    page = _referring_page.set(referer)
    try:
        yield
    finally:
        _referring_page.reset(page)
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

    Without an explicit `method`, a Request with a body (`body`, `json`, or `data`) is
    a POST, and one without is a GET.

    With the pinned Profile, the HTTP Fetcher sends the headers Chrome sends for the
    kind of request a Request is (`pernix.request_kinds`): a GET is a top-level
    navigation, a POST of a form a form submission, and anything else a `fetch()`,
    unless a `Sec-Fetch-Dest` header names the kind. Its `Referer` header names the
    page it comes from, from which the `Referer`, `Origin`, and `Sec-Fetch-Site` sent
    are made; an empty one names none. With `http.inherit_referer`, a Request
    constructed in a Callback without one takes the page the Response belongs to.

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
        render: Whether to load the page in the browser: `True`, or a `Render`
            with the options to load it with.
        fetcher: The name of the Fetcher to use; inferred when `None`.
        timeout: Seconds before the fetch fails; the setting applies when `None`.
        proxy: A proxy URL for this Request only, over those of the Run and of its
            Session; an empty one sends it without a proxy.
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
    render: bool | Render
    fetcher: str | None
    timeout: float | None
    proxy: str | None
    impersonate: str | None
    ext: Mapping[str, Mapping[str, Any]]

    def __init__(
        self,
        url: str,
        *,
        method: str | None = None,
        params: QueryParams | None = None,
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
        render: bool | Render = False,
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
            method: The HTTP method, in any case; `None` for POST when `body`, `json`,
                or `data` is given, and GET otherwise.
            params: Query parameters added to the URL, after those it has: strings
                and numbers, with a sequence of values repeating a name.
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
            render: Load the page in the browser: `True` with no options, or a
                `Render` with some, such as actions to run on the page.
            fetcher: The name of the Fetcher to use.
            timeout: Seconds before the fetch fails.
            proxy: A proxy URL for this Request only, over those of the Run and of
                its Session; an empty one sends it without a proxy.
            impersonate: A Profile override for this Request only.
            ext: Component-private data, by component name.

        Raises:
            ValueError: `url` is relative outside a Callback; more than one of `body`,
                `json`, and `data` is given; `cookies` is combined with a `Cookie`
                header; a cookie name is empty or has `=`, `;`, or a control
                character, or a cookie value has `;` or a control character;
                `timeout` is not positive; `Sec-Fetch-Dest` names an iframe or a load
                of something other than a GET; or a rendered Request names another
                destination than `document`.
            TypeError: `callback` or `errback` is not a method of an object, a
                value of `params` is neither a string nor a number, or `render` is
                neither a boolean nor a `Render`.
        """
        url = _absolute(url)
        if params is not None:
            url = _with_params(url, params)
        if sum(value is not None for value in (body, json, data)) > 1:
            raise ValueError("body, json, and data are mutually exclusive")
        if method is None:
            method = "GET" if body is None and json is None and data is None else "POST"
        request_headers = Headers(headers)
        if cookies and "Cookie" in request_headers:
            raise ValueError("pass cookies either as cookies= or as a Cookie header, not both")
        if cookies:
            check_cookies(cookies)
        if timeout is not None and timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        _check_render(render)
        for name, method_ in (("callback", callback), ("errback", errback)):
            if method_ is not None and not inspect.ismethod(method_):
                raise TypeError(f"{name} must be a method of the Spider, such as self.parse_item; got {method_!r}")

        if json is not None:
            body = orjson.dumps(json)
            request_headers = _with_default(request_headers, "Content-Type", _JSON_CONTENT_TYPE)
        elif data is not None:
            body = urlencode(data, doseq=True)
            request_headers = _with_default(request_headers, "Content-Type", _FORM_CONTENT_TYPE)
        check_kind(method.upper(), request_headers, render=bool(render))
        page = _referring_page.get()
        if page is not None:
            request_headers = _with_default(request_headers, "Referer", page)

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
                `body`; `json`, `data`, and `params` only exist when constructing a
                Request. The method stays as it is unless `method` is passed.

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


def _with_params(url: str, params: QueryParams) -> str:
    """Add query parameters to a URL, after its query and before its fragment."""
    encoded = urlencode(list(_query_pairs(params)))
    if not encoded:
        return url
    base, hash_mark, fragment = url.partition("#")
    _, question_mark, query = base.partition("?")  # a later "?" belongs to the query
    separator = "?" if not question_mark else "" if not query or query.endswith("&") else "&"
    return f"{base}{separator}{encoded}{hash_mark}{fragment}"


def _query_pairs(params: QueryParams) -> Iterator[tuple[str, str]]:
    items: Iterable[tuple[str, object]] = params.items() if isinstance(params, Mapping) else params
    for name, value in items:
        values: Iterable[object] = (
            value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else (value,)
        )
        for item in values:
            yield name, _query_value(name, item)


def _query_value(name: str, value: object) -> str:
    """A query parameter value as text; the type annotations are not enforced, so check it."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise TypeError(
            f"params[{name!r}] must be a string or a number, got {value!r}; spell booleans out, such as 'true'"
        )
    return str(value)


def _check_render(render: object) -> None:
    """Check `render`, as the annotations are not enforced."""
    if not isinstance(render, bool | Render):
        raise TypeError(f"render must be True, False, or a pernix.browser.Render; got {render!r}")


def _frozen[V](values: Mapping[str, V] | None) -> Mapping[str, V]:
    return MappingProxyType(dict(values or {}))


def _with_default(headers: Headers, name: str, value: str) -> Headers:
    return headers if name in headers else Headers([*headers.items(), (name, value)])
