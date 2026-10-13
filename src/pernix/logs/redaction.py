from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import cast

from pernix.settings import Secret

__all__ = ["HEADER_NAMES", "MASK", "SENSITIVE_NAMES", "Redactor", "revealed_secrets"]

MASK = "***"
"""Replaces every masked value."""

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

_USERINFO = re.compile(r"(?<=://)[^\s/?#@'\"<>]+@")


class Redactor:
    """Masks credentials and other sensitive values in log text.

    It masks:

    - the user and password in URLs, such as proxy URLs:
      `http://user:pass@proxy:8080` becomes `http://***@proxy:8080`;
    - the value of a sensitive name in query strings, forms, and cookies (`token=…`),
      in quoted pairs as reprs and JSON write them (`'Cookie': '…'`, `("Cookie", "…")`,
      `"password": "…"`), and after a sensitive header name (`Authorization: …`) up to
      the end of the line;
    - every occurrence of the given secret values, such as those of `Secret` settings.

    Names match case-insensitively. Masking is heuristic: it errs toward masking too
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
            r"""(?P<value>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|[^\s,)}\]]+)"""
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
        if self._secrets is not None:
            text = self._secrets.sub(MASK, text)
        text = _USERINFO.sub(f"{MASK}@", text)
        text = self._quoted.sub(_mask_quoted, text)
        text = self._assignments.sub(rf"\g<1>{MASK}", text)
        return self._headers.sub(rf"\g<1>{MASK}", text)


def revealed_secrets(values: Iterable[object]) -> frozenset[str]:
    """Collect the values of the `Secret`s in some values, including inside containers.

    Args:
        values: Values such as those of the settings.

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
    return frozenset(found)


def _alternation(names: Iterable[str]) -> str:
    # Longest first, so "x-api-key" is not matched as its suffix "key".
    return "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))


def _mask_quoted(match: re.Match[str]) -> str:
    value = match["value"]
    quote = value[0] if value[0] in "'\"" else ""
    return f"{match['key']}{quote}{MASK}{quote}"
