"""JSON answers of proxy APIs: where their proxies are, how each is written, and when it expires."""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import msgspec

from pernix.proxies.proxy import Proxy
from pernix.proxies.proxy_url import PROXY_SCHEMES, proxy_from_parts

__all__ = ["JsonAnswer", "read_json_answer"]

type ExpiresFormat = Literal["timestamp", "timestamp_ms", "datetime", "seconds"]
"""How an entry tells when its proxy expires: seconds or milliseconds since the epoch, an ISO 8601 date and time, or
the seconds left."""

_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})")
_MISSING = object()
"""What a path finds where an answer has nothing."""


class JsonAnswer(msgspec.Struct, frozen=True, forbid_unknown_fields=True, kw_only=True):
    """How to read the JSON answers of `proxies.api`: where its proxies are, how each is written, and when it expires.

    Paths are keys joined by dots, such as `data.proxy_list`; a key of digits
    indexes a list, such as `result.0.items`. Each entry of the list is a proxy: a
    string is its address, as `pernix.proxies.proxy_url()` takes it; an object is
    read through `address`, the path of its whole address, or else through `host`
    and `port`, and `username` and `password` if set. Its expiry, if `expires` names
    where it is, is written as `expires_format` says; an entry without it expires as
    `proxies.api_lifetime` says.

    Example:
        >>> JsonAnswer(list="data", host="ip", port="port", expires="deadline",
        ...     expires_format="datetime", timezone="+08:00")

    Attributes:
        list: The path of the list of proxies in the answer; empty for the answer
            itself.
        address: In an entry that is an object, the path of its whole address, such
            as `server`.
        host: In an entry that is an object, the path of its host, when `address` is
            not set; `port` is then needed too.
        port: The path of its port, a number or a string of digits.
        username: The path of the user it authenticates as, if any.
        password: The path of its password, if any.
        scheme: The scheme of the proxies whose entries give their host and port.
        expires: In an entry that is an object, the path of when its proxy expires;
            `None` when the answer does not tell.
        expires_format: How `expires` is written: `timestamp`, seconds since the
            epoch; `timestamp_ms`, milliseconds; `datetime`, an ISO 8601 date and
            time, such as `2026-01-01 12:00:00`, with or without an offset; or
            `seconds`, the seconds left once the answer arrived.
        timezone: The time zone of a `datetime` written without an offset, which
            `datetime` needs: an IANA name, such as `Asia/Shanghai`, or a fixed
            offset, such as `+08:00`. A value's own offset wins.
    """

    list: str = ""
    address: str | None = None
    host: str | None = None
    port: str | None = None
    username: str | None = None
    password: str | None = None
    scheme: str = "http"
    expires: str | None = None
    expires_format: ExpiresFormat = "timestamp"
    timezone: str | None = None

    def __post_init__(self) -> None:
        problems: list[str] = []
        if self.address is not None and self.host is not None:
            problems.append("address and host: an entry is read through one or the other")
        if (self.host is None) != (self.port is None):
            problems.append("host and port: an entry's host needs its port, and its port its host")
        if self.scheme not in PROXY_SCHEMES:
            problems.append(f"scheme: one of {', '.join(sorted(PROXY_SCHEMES))}")
        if self.expires is not None and self.expires_format == "datetime" and self.timezone is None:
            problems.append(
                "timezone: a datetime written without an offset needs its time zone, such as Asia/Shanghai or +08:00"
            )
        if self.timezone is not None:
            try:
                _time_zone(self.timezone)
            except ValueError as exc:
                problems.append(f"timezone: {exc}")
        if problems:
            raise ValueError("; ".join(problems))


def read_json_answer(text: str, answer: JsonAnswer, *, now: float) -> tuple[list[Proxy], list[str]]:
    """Read the proxies of a JSON answer, as `answer` maps them.

    Args:
        text: The answer.
        answer: Where its proxies are, and how each is written.
        now: When the answer arrived, in seconds since the epoch, from which the
            seconds left are counted.

    Returns:
        The proxies, in order, each with its expiry if the answer tells it, their
        URLs not checked yet; and the problems found, each naming a path, or an entry
        by its position, never a value. An answer that is not JSON, or has no list
        where `answer.list` says, gives no proxy, and that one problem.
    """
    try:
        whole = msgspec.json.decode(text)
    except msgspec.DecodeError:  # numbers out of range included
        return [], ["it is not JSON"]
    entries = _at(whole, answer.list)
    if entries is _MISSING:
        return [], [f"it has no `{answer.list}`"]
    if not isinstance(entries, list):
        return [], [f"`{answer.list or 'the answer'}` is not a list"]
    proxies: list[Proxy] = []
    problems: list[str] = []
    for position, entry in enumerate(cast("list[object]", entries), start=1):
        try:
            proxies.append(_proxy(entry, answer, now))
        except ValueError as exc:
            problems.append(f"#{position} ({exc})")
    return proxies, problems


def _at(value: object, path: str | None) -> object:
    """Return what a path finds in a JSON value; `_MISSING` where it has nothing.

    Args:
        value: The JSON value, as decoded.
        path: Keys joined by dots, a key of digits indexing a list; empty or `None`
            for the value itself.

    Returns:
        The value the path leads to.
    """
    if not path:
        return value
    for key in path.split("."):
        if isinstance(value, dict) and key in value:
            value = cast("dict[str, object]", value)[key]
        elif isinstance(value, list) and key.isdigit() and int(key) < len(cast("list[object]", value)):
            value = cast("list[object]", value)[int(key)]
        else:
            return _MISSING
    return value


def _time_zone(name: str) -> dt.tzinfo:
    """Return the time zone an IANA name, or a fixed offset such as `+08:00`, names.

    Args:
        name: The name, or the offset; `UTC` and `Z` need no time zone data.

    Returns:
        The time zone.

    Raises:
        ValueError: No time zone is named so here, as where the system has no time
            zone data, such as on Windows without the `tzdata` package.
    """
    if name in ("UTC", "Z"):
        return dt.UTC
    offset = _OFFSET.fullmatch(name)
    if offset is not None:
        sign, hours, minutes = offset.groups()
        delta = dt.timedelta(hours=int(hours), minutes=int(minutes))
        return dt.timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"no time zone is named {name!r} here; give a fixed offset, such as +08:00, or install the tzdata "
            "package where the system has no time zone data, as on Windows"
        ) from None


def _proxy(entry: object, answer: JsonAnswer, now: float) -> Proxy:
    """The proxy an entry of the answer gives, with its expiry if it tells it.

    Raises:
        ValueError: The entry lacks a field it needs, or holds one of the wrong
            kind; the message names the field, never its value.
    """
    if isinstance(entry, str):
        return Proxy(entry)
    if not isinstance(entry, dict):
        raise ValueError("neither a string nor an object")
    fields = cast("dict[str, object]", entry)
    if answer.address is not None:
        url = _text(fields, answer.address)
    elif answer.host is not None and answer.port is not None:
        port = _at(fields, answer.port)
        if isinstance(port, bool) or not isinstance(port, (int, str)) or not str(port).isdigit():
            raise ValueError(_field(answer.port, port))
        url = proxy_from_parts(
            _text(fields, answer.host),
            port,
            scheme=answer.scheme,
            username=_optional_text(fields, answer.username),
            password=_optional_text(fields, answer.password),
        )
    else:
        raise ValueError("an object, but neither address nor host is set to read it")
    return Proxy(url, expires=_expires(fields, answer, now))


def _expires(entry: object, answer: JsonAnswer, now: float) -> float | None:
    """When an entry's proxy expires, in seconds since the epoch; `None` if it does not tell."""
    if answer.expires is None:
        return None
    value = _at(entry, answer.expires)
    if value is _MISSING or value is None:
        return None
    try:
        match answer.expires_format:
            case "timestamp" | "timestamp_ms" | "seconds":
                number = _number(value)
                if answer.expires_format == "timestamp_ms":
                    return number / 1000
                return now + number if answer.expires_format == "seconds" else number
            case "datetime":
                if not isinstance(value, str):
                    raise TypeError(value)
                when = dt.datetime.fromisoformat(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_time_zone(answer.timezone or "UTC"))
                return when.timestamp()
    except (TypeError, ValueError):
        raise ValueError(f"`{answer.expires}` is not a {answer.expires_format}") from None


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(value)
    number = float(value)
    if not math.isfinite(number):  # such as NaN, which would never expire
        raise ValueError(value)
    return number


def _text(entry: object, path: str) -> str:
    value = _at(entry, path)
    if not isinstance(value, str) or not value:
        raise ValueError(_field(path, value))
    return value


def _optional_text(entry: object, path: str | None) -> str | None:
    if path is None:
        return None
    value = _at(entry, path)
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"`{path}` is not a string")
    return value


def _field(path: str, value: object) -> str:
    """Why a field cannot be read: it is missing, or of the wrong kind; never its value."""
    return f"no `{path}`" if value is _MISSING else f"`{path}` is not as expected"
