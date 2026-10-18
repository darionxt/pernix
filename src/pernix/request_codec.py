"""Persisting Requests for durable Frontiers (ADR 0004).

Requests are encoded as msgpack with a schema version. Callbacks are stored by the
name of the Spider method and looked up again when decoding; a missing method is an
error, never a fallback to `parse`. User data (`kwargs`, `meta`, `ext`) must be
JSON-compatible: `None`, booleans, numbers (integers within 64 bits), strings, lists,
and mappings with string keys. Tuples come back as lists, and mappings as dicts.
`Render` options are kept with their actions, each tagged with its kind. Nothing is
ever pickled.

`REQUEST_SCHEMA_VERSION` changes only for incompatible changes. Fields added later
have defaults, and fields a decoder does not know are ignored, so older and newer
versions can read each other's additive changes.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import msgspec

from pernix.browser import Action, Render  # msgspec resolves the field types at runtime
from pernix.errors import SerializationError
from pernix.request import Request

if TYPE_CHECKING:
    from pernix.browser import ResourceType
    from pernix.request import Callback

__all__ = ["REQUEST_SCHEMA_VERSION", "decode_request", "encode_request"]

REQUEST_SCHEMA_VERSION = 1
"""The version of the persisted Request format this Pernix writes and reads."""


class _RenderRecord(msgspec.Struct, kw_only=True, omit_defaults=True):
    wait_for: str | None = None
    actions: tuple[Action, ...] = ()
    block: tuple[str, ...] | None = None
    capture: tuple[str, ...] = ()
    screenshot: bool = False


class _RequestRecord(msgspec.Struct, kw_only=True, omit_defaults=True):
    schema_version: int
    url: str
    method: str = "GET"
    headers: list[tuple[str, str]] = msgspec.field(default_factory=list[tuple[str, str]])
    body: bytes | None = None
    callback: str | None = None
    errback: str | None = None
    kwargs: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])
    meta: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])
    priority: int = 0
    dont_filter: bool = False
    fingerprint: str | None = None
    cookies: dict[str, str] = msgspec.field(default_factory=dict[str, str])
    session: str | None = None
    render: bool | _RenderRecord = False
    fetcher: str | None = None
    timeout: float | None = None
    proxy: str | None = None
    impersonate: str | None = None
    ext: dict[str, dict[str, Any]] = msgspec.field(default_factory=dict[str, dict[str, Any]])


_MIN_INT = -(2**63)
_MAX_INT = 2**64 - 1

_encoder = msgspec.msgpack.Encoder()
_decoder = msgspec.msgpack.Decoder(_RequestRecord)


def encode_request(request: Request, spider: object) -> bytes:
    """Serialize a Request for a persistent Frontier.

    Args:
        request: The Request to persist.
        spider: The Spider whose methods the Callback and errback must be.

    Returns:
        The encoded Request.

    Raises:
        SerializationError: The Callback or errback is not a method of `spider`, or
            `kwargs`, `meta`, or `ext` holds a value that is not JSON-compatible or an
            integer outside 64 bits; the message names its key.
    """
    kwargs = _json_object("kwargs", request.kwargs)
    meta = _json_object("meta", request.meta)
    ext = {component: _json_object(f"ext[{component!r}]", data) for component, data in request.ext.items()}
    record = _RequestRecord(
        schema_version=REQUEST_SCHEMA_VERSION,
        url=request.url,
        method=request.method,
        headers=request.headers.items(),
        body=request.body,
        callback=_method_name(request.callback, spider, "callback"),
        errback=_method_name(request.errback, spider, "errback"),
        kwargs=kwargs,
        meta=meta,
        priority=request.priority,
        dont_filter=request.dont_filter,
        fingerprint=request.fingerprint,
        cookies=dict(request.cookies),
        session=request.session,
        render=_render_record(request.render),
        fetcher=request.fetcher,
        timeout=request.timeout,
        proxy=request.proxy,
        impersonate=request.impersonate,
        ext=ext,
    )
    return _encoder.encode(record)


def decode_request(data: bytes, spider: object) -> Request:
    """Restore a Request persisted by `encode_request()`.

    Args:
        data: The encoded Request.
        spider: The Spider whose methods the Callback and errback are looked up on.

    Returns:
        The Request.

    Raises:
        SerializationError: The data is not a persisted Request, it was written in a
            newer incompatible format, its `Render` options are invalid, or the
            Spider has no method by the stored Callback or errback name.
    """
    try:
        record = _decoder.decode(data)
    except msgspec.MsgspecError as exc:
        raise SerializationError(f"not a valid persisted Request: {exc}") from exc
    if record.schema_version > REQUEST_SCHEMA_VERSION:
        raise SerializationError(
            f"the Request was persisted in format {record.schema_version}, but this Pernix "
            f"reads format {REQUEST_SCHEMA_VERSION} and older; upgrade Pernix to resume this Run"
        )
    render = record.render if isinstance(record.render, bool) else _restored_render(record.render)
    return Request(
        record.url,
        method=record.method,
        headers=record.headers,
        body=record.body,
        callback=_resolve_method(spider, record.callback, "callback"),
        errback=_resolve_method(spider, record.errback, "errback"),
        kwargs=record.kwargs,
        meta=record.meta,
        priority=record.priority,
        dont_filter=record.dont_filter,
        fingerprint=record.fingerprint,
        cookies=record.cookies,
        session=record.session,
        render=render,
        fetcher=record.fetcher,
        timeout=record.timeout,
        proxy=record.proxy,
        impersonate=record.impersonate,
        ext=record.ext,
    )


def _render_record(render: bool | Render) -> bool | _RenderRecord:
    if isinstance(render, bool):
        return render
    return _RenderRecord(
        wait_for=render.wait_for,
        actions=render.actions,
        block=render.block,
        capture=render.capture,
        screenshot=render.screenshot,
    )


def _restored_render(record: _RenderRecord) -> Render:
    try:
        return Render(
            wait_for=record.wait_for,
            actions=record.actions,
            block=cast("tuple[ResourceType, ...] | None", record.block),
            capture=record.capture,
            screenshot=record.screenshot,
        )
    except ValueError as exc:
        raise SerializationError(f"the persisted Request's Render options are invalid: {exc}") from exc


def _method_name(method: Callback | None, spider: object, role: str) -> str | None:
    if method is None:
        return None
    if not inspect.ismethod(method) or method.__self__ is not spider:
        raise SerializationError(f"the {role} {method!r} is not a method of the Spider {type(spider).__name__}")
    found = getattr(spider, method.__name__, None)
    if not inspect.ismethod(found) or found.__func__ is not method.__func__:
        raise SerializationError(
            f"the {role} {method.__qualname__} cannot be found again as {type(spider).__name__}.{method.__name__}"
        )
    return method.__name__


def _resolve_method(spider: object, name: str | None, role: str) -> Callback | None:
    if name is None:
        return None
    method = getattr(spider, name, None)
    if not inspect.ismethod(method):
        raise SerializationError(
            f"the persisted Request's {role} {name!r} is not a method of {type(spider).__name__}; "
            "was it renamed or removed?"
        )
    return method


def _json_object(path: str, value: Mapping[str, Any]) -> dict[str, Any]:
    return cast("dict[str, Any]", _to_json(path, value))


def _to_json(path: str, value: object) -> object:
    """Copy a value as plain JSON-compatible data, or raise naming the offending key."""
    match value:
        case bool() | None | float() | str():
            return value
        case int():
            if not _MIN_INT <= value <= _MAX_INT:
                raise SerializationError(f"{path} holds the integer {value}, which does not fit in 64 bits")
            return value
        case Mapping():
            plain: dict[str, object] = {}
            for key, item in cast("Mapping[object, object]", value).items():
                if not isinstance(key, str):
                    raise SerializationError(f"{path} has the key {key!r}; keys must be strings")
                plain[key] = _to_json(f"{path}[{key!r}]", item)
            return plain
        case list() | tuple():
            items = cast("list[object] | tuple[object, ...]", value)
            return [_to_json(f"{path}[{index}]", item) for index, item in enumerate(items)]
        case _:
            raise SerializationError(f"{path} holds a {type(value).__name__}, which is not JSON-compatible")
