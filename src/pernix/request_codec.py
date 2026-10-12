"""Persisting Requests for durable Frontiers (ADR 0004).

Requests are encoded as msgpack with a schema version. Callbacks are stored by the
name of the Spider method and looked up again when decoding; a missing method is an
error, never a fallback to `parse`. User data (`kwargs`, `meta`, `ext`) must be
JSON-compatible: `None`, booleans, numbers, strings, lists, and dicts with string
keys. Tuples come back as lists. Nothing is ever pickled.

`REQUEST_SCHEMA_VERSION` changes only for incompatible changes. Fields added later
have defaults, and fields a decoder does not know are ignored, so older and newer
versions can read each other's additive changes.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

import msgspec

from pernix.errors import SerializationError
from pernix.request import Request

if TYPE_CHECKING:
    from pernix.request import Callback

__all__ = ["REQUEST_SCHEMA_VERSION", "decode_request", "encode_request"]

REQUEST_SCHEMA_VERSION = 1
"""The version of the persisted Request format this Pernix writes and reads."""


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
    render: bool = False
    fetcher: str | None = None
    timeout: float | None = None
    proxy: str | None = None
    impersonate: str | None = None
    ext: dict[str, dict[str, Any]] = msgspec.field(default_factory=dict[str, dict[str, Any]])


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
            `kwargs`, `meta`, or `ext` holds a value that is not JSON-compatible; the
            message names its key.
    """
    _check_json("kwargs", request.kwargs)
    _check_json("meta", request.meta)
    for component, data in request.ext.items():
        _check_json(f"ext[{component!r}]", data)
    record = _RequestRecord(
        schema_version=REQUEST_SCHEMA_VERSION,
        url=request.url,
        method=request.method,
        headers=request.headers.items(),
        body=request.body,
        callback=_method_name(request.callback, spider, "callback"),
        errback=_method_name(request.errback, spider, "errback"),
        kwargs=dict(request.kwargs),
        meta=dict(request.meta),
        priority=request.priority,
        dont_filter=request.dont_filter,
        fingerprint=request.fingerprint,
        cookies=dict(request.cookies),
        session=request.session,
        render=request.render,
        fetcher=request.fetcher,
        timeout=request.timeout,
        proxy=request.proxy,
        impersonate=request.impersonate,
        ext={component: dict(data) for component, data in request.ext.items()},
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
            newer incompatible format, or the Spider has no method by the stored
            Callback or errback name.
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
        render=record.render,
        fetcher=record.fetcher,
        timeout=record.timeout,
        proxy=record.proxy,
        impersonate=record.impersonate,
        ext=record.ext,
    )


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


def _check_json(path: str, value: object) -> None:
    match value:
        case None | bool() | int() | float() | str():
            return
        case Mapping():
            for key, item in cast("Mapping[object, object]", value).items():
                if not isinstance(key, str):
                    raise SerializationError(f"{path} has the key {key!r}; keys must be strings")
                _check_json(f"{path}[{key!r}]", item)
        case list() | tuple():
            for index, item in enumerate(cast("list[object] | tuple[object, ...]", value)):
                _check_json(f"{path}[{index}]", item)
        case _:
            raise SerializationError(f"{path} holds a {type(value).__name__}, which is not JSON-compatible")
