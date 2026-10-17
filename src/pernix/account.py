"""Accounts: login credentials for a target site, which the Session bound to one logs in with."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
import orjson

from pernix.errors import SettingsError
from pernix.secret import Secret

if TYPE_CHECKING:
    import os
    from collections.abc import AsyncIterable, Iterable, Iterator

__all__ = ["ACCOUNT_FILE_SUFFIXES", "Account", "collect_accounts", "read_accounts"]

ACCOUNT_FILE_SUFFIXES = frozenset({".csv", ".jsonl", ".ndjson", ".jl"})
"""The suffixes of the files Accounts are read from: CSV, or JSON Lines."""

_FIELDS = frozenset({"id", "username", "password", "max_concurrency"})
"""The CSV columns that fill a field of their own; the others are `extra` credentials."""


class Account(msgspec.Struct, frozen=True, kw_only=True, repr_omit_defaults=True):
    """Login credentials for a target site, which the Session bound to it logs in with.

    The password and the `extra` credentials are `Secret`s, given as such or as
    strings, which are wrapped, though type checkers expect `Secret`s; their values
    are masked in the Run's logs. The username and the id tell Accounts apart in
    logs, and are not masked.

    Attributes:
        username: The name to log in with.
        password: The password.
        id: Names the Account and the Session bound to it, as `Request.session`
            names it; the username unless given.
        extra: Further credentials by name, such as a TOTP seed.
        max_concurrency: Fetches in progress at once under its Session.

    Example:
        >>> Account(username="alice@example.com", password=Secret("pw"), id="alice")
        Account(username='alice@example.com', password=Secret('**********'), id='alice')
    """

    username: str
    password: Secret
    id: str = ""
    extra: dict[str, Secret] = {}
    max_concurrency: Annotated[int, msgspec.Meta(ge=1)] = 1

    def __post_init__(self) -> None:
        if not self.username:
            raise ValueError("an Account needs a username")
        if self.max_concurrency < 1:
            raise ValueError(f"the max_concurrency of an Account must be at least 1, not {self.max_concurrency}")
        msgspec.structs.force_setattr(self, "password", _secret(self.password))
        extra: dict[str, object] = dict(self.extra)  # strings given for Secrets too
        if not all(isinstance(value, Secret) for value in extra.values()):
            msgspec.structs.force_setattr(self, "extra", {name: _secret(value) for name, value in extra.items()})
        if not self.id:
            msgspec.structs.force_setattr(self, "id", self.username)


def read_accounts(path: str | os.PathLike[str]) -> list[Account]:
    """Read Accounts from a JSON Lines file, one object per line, or from a CSV file with a header row.

    The suffix tells the format: `.csv`, or `.jsonl`, `.ndjson`, or `.jl`. Each
    JSON object has the fields of an `Account`. In a CSV file, the columns `id`,
    `username`, `password`, and `max_concurrency` fill those fields, and every
    other column a credential in `extra`; empty cells are left out. Blank lines are
    skipped. No value is ever quoted in an error.

    Args:
        path: The file.

    Returns:
        The Accounts, in the order of the file.

    Raises:
        pernix.errors.SettingsError: The file cannot be read, or has another
            suffix; or some lines hold no valid Account, each of which it names.
    """
    file = Path(path)
    suffix = file.suffix.lower()
    if suffix not in ACCOUNT_FILE_SUFFIXES:
        raise SettingsError([f"{file}: Accounts are read from {_suffixes()} files"])
    try:
        with file.open(encoding="utf-8-sig", newline="") as stream:
            records = list(_csv_records(stream) if suffix == ".csv" else _json_records(stream))
    except OSError as exc:
        raise SettingsError([f"{file}: cannot read the Accounts: {exc.strerror or exc}"]) from None
    except csv.Error as exc:
        raise SettingsError([f"{file}: not a valid CSV file: {exc}"]) from None
    accounts: list[Account] = []
    problems: list[str] = []
    for line, record in records:
        if isinstance(record, _Problem):
            problems.append(f"{file}, line {line}: {record.reason}")
            continue
        try:
            # CSV cells are all text, which is converted to the field types.
            accounts.append(msgspec.convert(record, Account, strict=suffix != ".csv", dec_hook=_decode_secret))
        except (msgspec.ValidationError, ValueError) as exc:
            problems.append(f"{file}, line {line}: {exc}")
    if problems:
        raise SettingsError(problems)
    return accounts


async def collect_accounts(accounts: AsyncIterable[object]) -> tuple[Account, ...]:
    """Collect the Accounts a Spider gives, such as with `Spider.accounts()`.

    Args:
        accounts: The Accounts.

    Returns:
        The Accounts, in order.

    Raises:
        pernix.errors.SettingsError: Something else than an `Account` was given, or
            two Accounts have the same id; it names every problem found.
    """
    collected: dict[str, Account] = {}
    problems: list[str] = []
    async for account in accounts:
        if not isinstance(account, Account):
            problems.append(f"Accounts must be pernix.Account objects, not {type(account).__name__}")
        elif account.id in collected:
            problems.append(f"two Accounts have the id {account.id!r}")
        else:
            collected[account.id] = account
    if problems:
        raise SettingsError(problems)
    return tuple(collected.values())


def _secret(value: object) -> Secret:
    return value if isinstance(value, Secret) else Secret(str(value))


def _decode_secret(target: object, value: object) -> object:
    if target is Secret and isinstance(value, str):
        return Secret(value)
    raise TypeError(f"Expected `{getattr(target, '__name__', target)}`, got `{type(value).__name__}`")


def _json_records(lines: Iterable[str]) -> Iterator[tuple[int, object]]:
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            yield number, orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            yield number, _Problem(f"invalid JSON: {exc.msg}")


def _csv_records(lines: Iterable[str]) -> Iterator[tuple[int, object]]:
    reader = csv.DictReader(lines)
    for row in reader:
        if None in row:  # cells past the header's columns
            yield reader.line_num, _Problem("more cells than the header has columns")
            continue
        record: dict[str, Any] = {column: cell for column, cell in row.items() if cell and column in _FIELDS}
        extra = {column: cell for column, cell in row.items() if cell and column not in _FIELDS}
        if extra:
            record["extra"] = extra
        yield reader.line_num, record


@dataclass(frozen=True, slots=True)
class _Problem:
    """Why a line of an Accounts file holds no record."""

    reason: str


def _suffixes() -> str:
    return ", ".join(sorted(ACCOUNT_FILE_SUFFIXES))
