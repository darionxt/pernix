from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix import Account
from pernix.account import collect_accounts, read_accounts
from pernix.errors import SettingsError
from pernix.settings import Secret, SessionSettings, load_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def test_strings_given_for_credentials_become_secrets_and_the_id_defaults_to_the_username() -> None:
    account = Account(username="alice@shop.example", password="pw", extra={"totp": "seed"})  # type: ignore[arg-type]

    assert account.id == "alice@shop.example"
    assert (account.password, account.extra["totp"]) == (Secret("pw"), Secret("seed"))
    assert repr(account) == (
        "Account(username='alice@shop.example', password=Secret('**********'), id='alice@shop.example', "
        "extra={'totp': Secret('**********')})"
    )


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"username": ""}, "needs a username"),
        ({"max_concurrency": 0}, "at least 1"),
    ],
)
def test_an_account_needs_a_username_and_some_concurrency(fields: dict[str, object], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        Account(**{"username": "alice", "password": Secret("pw"), **fields})  # type: ignore[arg-type]


def test_accounts_are_settings_given_as_tables() -> None:
    settings = load_settings(
        spider_settings={
            "sessions.enabled": True,
            "sessions.accounts": [{"username": "alice", "password": "pw", "id": "a", "max_concurrency": 2}],
        }
    )

    assert settings.section(SessionSettings).accounts == (
        Account(username="alice", password=Secret("pw"), id="a", max_concurrency=2),
    )


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ({"sessions.accounts": [{"username": "alice", "password": "pw"}]}, "enable one with sessions.enabled"),
        ({"sessions.enabled": True, "sessions.accounts_file": "accounts.txt"}, "not 'accounts.txt'"),
    ],
)
def test_accounts_need_a_session_pool_and_a_known_file_format(given: dict[str, object], problem: str) -> None:
    with pytest.raises(SettingsError, match=problem):
        load_settings(spider_settings=given)


def test_accounts_are_read_from_json_lines(tmp_path: Path) -> None:
    file = tmp_path / "accounts.jsonl"
    file.write_text(
        '{"username": "alice", "password": "pw", "extra": {"totp": "seed"}}\n\n'
        '{"username": "bob", "password": "pw2", "id": "b", "max_concurrency": 3}\n'
    )

    assert read_accounts(file) == [
        Account(username="alice", password=Secret("pw"), extra={"totp": Secret("seed")}),
        Account(username="bob", password=Secret("pw2"), id="b", max_concurrency=3),
    ]


def test_accounts_are_read_from_csv_with_other_columns_as_extra_credentials(tmp_path: Path) -> None:
    file = tmp_path / "accounts.csv"
    file.write_text("id,username,password,max_concurrency,totp\nalice,alice@shop.example,pw,2,seed\n,bob,pw2,,\n")

    assert read_accounts(file) == [
        Account(
            username="alice@shop.example",
            password=Secret("pw"),
            id="alice",
            max_concurrency=2,
            extra={"totp": Secret("seed")},
        ),
        Account(username="bob", password=Secret("pw2")),
    ]


def test_every_invalid_line_is_named_without_quoting_a_value(tmp_path: Path) -> None:
    file = tmp_path / "accounts.jsonl"
    file.write_text(
        '{"username": "alice", "password": "s3cr3t", "max_concurrency": "x"}\nnot json\n{"password": "pw"}\n'
    )

    with pytest.raises(SettingsError) as raised:
        read_accounts(file)

    assert [problem.removeprefix(f"{file}, ") for problem in raised.value.problems] == [
        "line 1: Expected `int`, got `str` - at `$.max_concurrency`",
        "line 2: invalid JSON: invalid literal, expected 'null'",
        "line 3: Object missing required field `username`",
    ]
    assert "s3cr3t" not in str(raised.value)


def test_a_csv_row_with_more_cells_than_columns_is_refused(tmp_path: Path) -> None:
    file = tmp_path / "accounts.csv"
    file.write_text("username,password\nalice,pw,extra\n")

    with pytest.raises(SettingsError, match="line 2: more cells than the header has columns"):
        read_accounts(file)


def test_a_missing_file_is_a_settings_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read the Accounts"):
        read_accounts(tmp_path / "missing.csv")


async def test_collected_accounts_must_be_accounts_with_distinct_ids() -> None:
    async def given() -> AsyncIterator[object]:
        yield Account(username="alice", password=Secret("pw"))
        yield {"username": "bob"}
        yield Account(username="other", password=Secret("pw"), id="alice")

    with pytest.raises(SettingsError) as raised:
        await collect_accounts(given())

    assert raised.value.problems == [
        "Accounts must be pernix.Account objects, not dict",
        "two Accounts have the id 'alice'",
    ]
