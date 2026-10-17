from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import cast

import msgspec

from pernix.settings import Secret

__all__ = ["ELIDED", "HEADER_NAMES", "MASK", "SENSITIVE_NAMES", "Redactor", "revealed_secrets"]

MASK = "***"
"""Replaces every masked value."""

ELIDED = "..."
"""Replaces a container that `Redactor.redact_value()` does not descend into."""

_MAX_DEPTH = 100

HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
        "x-amz-security-token",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
    }
)
"""Header names whose value is masked to the end of a `Name: value` line."""

SENSITIVE_NAMES = HEADER_NAMES | {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "client_secret",
    "id_token",
    "jsessionid",
    "key",
    "passwd",
    "password",
    "phpsessid",
    "pwd",
    "refresh_token",
    "secret",
    "session",
    "sessionid",
    "sid",
    "sig",
    "signature",
    "token",
    "x-amz-credential",
    "x-amz-signature",
}
"""Names whose values are masked in `name=value` pairs and in quoted `'name': value` pairs."""

_USERINFO = re.compile(r"(?<=://)[^\s/?#@'\"<>]+(?=@)")
# Without a scheme, only a user and a password joined by a colon count, either possibly empty: the colon tells them
# from an e-mail address, whose local part has none. A mailto: address is the exception. The user takes the characters
# of the password but the colon, so it is masked whole, with whatever is joined to it.
_BARE_USERINFO = re.compile(r"(?<![^\s/?#@'\"<>:])(?!(?i:mailto):)[^\s/?#@'\"<>:]*:[^\s/?#@'\"<>]*(?=@[\w\[])")


class Redactor:
    """Masks credentials and other sensitive values in log text.

    It masks:

    - the user and password in URLs, such as proxy URLs:
      `http://user:pass@proxy:8080` becomes `http://***@proxy:8080`, and in proxies
      given without a scheme, as curl takes them: `user:pass@proxy:8080` becomes
      `***@proxy:8080`. Without a scheme, only a user and a password joined by a colon
      before `@` and a host are masked, with whatever is joined to them, as `proxy=` in
      `proxy=user:pass@proxy`. E-mail addresses are kept, `mailto:` ones included,
      unless a word is joined to them by a colon (`contact:a@shop.example`);
    - the value of a sensitive name in query strings, forms, and cookies (`token=…`),
      in quoted pairs as reprs and JSON write them (`'Cookie': '…'`, `("Cookie", "…")`,
      `"password": "…"`), and after a sensitive header name (`Authorization: …`) up to
      the end of the line;
    - every occurrence of the given secret values, such as those of `Secret` settings.

    Names match case-insensitively. Every rule looks at the text as given, and again
    with the secret values starred out, and values that several rules find, overlapping
    or side by side, are masked as one. Masking is heuristic: it errs toward masking too
    much rather than too little.

    Example:
        >>> Redactor().redact("via http://alice:pw@proxy:8080/?token=abc")
        'via http://***@proxy:8080/?token=***'
    """

    def __init__(self, *, names: Iterable[str] = (), secrets: Iterable[str] = ()) -> None:
        """Compile the masking rules.

        Args:
            names: Names to mask besides `SENSITIVE_NAMES`, in every form, including
                `Name: value` lines.
            secrets: Values to mask wherever they occur; empty ones are ignored.
        """
        extra = {name.strip().lower() for name in names} - {""}
        fields = _alternation(SENSITIVE_NAMES | extra)
        headers = _alternation(HEADER_NAMES | extra)
        self._assignments = re.compile(rf"(?i)(?<![\w-])((?:{fields})=)[^&#;\s'\",)}}\]]+")
        self._quoted = re.compile(
            rf"""(?i)(?<![\w-])(?P<key>(?P<quote>['"])(?:{fields})(?P=quote)\s*[:,]\s*)"""
            r"""(?P<value>'(?P<single>(?:[^'\\]|\\.)*)'|"(?P<double>(?:[^"\\]|\\.)*)"|[^\s,)}\]]+)"""
        )
        self._headers = re.compile(rf"(?im)(?<![\w'\"-])((?:{headers}):[ \t]*)[^\s][^\r\n]*")
        values = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        self._secrets = re.compile("|".join(map(re.escape, values))) if values else None

    def redact(self, text: str) -> str:
        """Return `text` with its sensitive values masked.

        Args:
            text: A log message, an exception text, or another string.

        Returns:
            The masked text.
        """
        spans = self._sensitive_spans(text)
        if self._secrets is not None:
            spans += [match.span() for match in self._secrets.finditer(text)]
            # Once more with the secret values starred out, which keeps every offset: a name right after one then
            # stands apart, while one that overlaps it was found above.
            starred = self._secrets.sub(lambda match: "*" * len(match[0]), text)
            spans += self._sensitive_spans(starred)
        return _masked(text, spans)

    def _sensitive_spans(self, text: str) -> list[tuple[int, int]]:
        # Every rule looks at the same text, so that masking a value never hides another from a rule.
        spans = [match.span() for match in _USERINFO.finditer(text)]
        spans.extend((match.end(1), match.end()) for match in self._assignments.finditer(text))
        headers = list(self._headers.finditer(text))
        spans.extend((match.end(1), match.end()) for match in headers)
        quoted = list(self._quoted.finditer(text))
        spans.extend(map(_quoted_value, quoted))
        # A header name or quoted name that a user without a scheme starts in is kept.
        names = [match.span(1) for match in headers] + [match.span("key") for match in quoted]
        for match in _BARE_USERINFO.finditer(text):
            start, end = match.span()
            for name_start, name_end in names:
                if name_start <= start < name_end:
                    start = min(name_end, end)
            spans.append((start, end))
        return spans

    def redact_value(self, value: object) -> object:
        """Return `value` with the strings in it masked, including inside containers.

        Mappings, lists, tuples, sets, and frozensets are copied with their string
        keys and items masked; `value` itself is never changed. A container inside
        itself, or nested deeper than 100 levels, is replaced by `"..."`. Other values
        are returned as they are.

        Args:
            value: A value such as an extra field of a log record.

        Returns:
            The masked string, a masked copy of the container, or `value`.
        """
        return self._redact_value(value, ())

    def _redact_value(self, value: object, enclosing: tuple[int, ...]) -> object:
        if isinstance(value, str):
            return self.redact(value)
        identity = id(value)
        if not isinstance(value, Mapping | list | tuple | set | frozenset):
            return value
        if identity in enclosing or len(enclosing) >= _MAX_DEPTH:
            return ELIDED
        inside = (*enclosing, identity)
        if isinstance(value, Mapping):
            entries = cast("Mapping[object, object]", value)
            return {self._redact_value(key, inside): self._redact_value(item, inside) for key, item in entries.items()}
        items = [self._redact_value(item, inside) for item in cast("Iterable[object]", value)]
        if isinstance(value, tuple):
            return tuple(items)
        if isinstance(value, frozenset):
            return frozenset(items)
        if isinstance(value, set):
            return set(items)
        return items


def revealed_secrets(values: Iterable[object]) -> frozenset[str]:
    """Collect the values of the `Secret`s in some values, including inside containers and msgspec Structs.

    Args:
        values: Values such as those of the settings, or `pernix.Account`s.

    Returns:
        The revealed values of every `Secret` found.
    """
    found: set[str] = set()
    pending = list(values)
    while pending:
        value = pending.pop()
        if isinstance(value, Secret):
            found.add(value.reveal())
        elif isinstance(value, Mapping):
            entries = cast("Mapping[object, object]", value)
            pending.extend(entries.keys())
            pending.extend(entries.values())
        elif isinstance(value, list | tuple | set | frozenset):
            pending.extend(cast("Iterable[object]", value))
        elif isinstance(value, msgspec.Struct):
            pending.extend(getattr(value, name) for name in value.__struct_fields__)
    return frozenset(found)


def _alternation(names: Iterable[str]) -> str:
    # Longest first, so "x-api-key" is not matched as its suffix "key".
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))


def _quoted_value(match: re.Match[str]) -> tuple[int, int]:
    # Inside its quotes, unless it lacks the closing one, as a value cut short does: then all of it.
    for inner in ("single", "double"):
        if match[inner] is not None:
            return match.span(inner)
    return match.span("value")


def _masked(text: str, spans: Iterable[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:  # overlapping or side by side, masked as one
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[str] = []
    position = 0
    for start, end in merged:
        parts += (text[position:start], MASK)
        position = end
    parts.append(text[position:])
    return "".join(parts)
