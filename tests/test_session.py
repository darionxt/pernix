import logging
from collections.abc import Callable

import msgspec
import pytest

from pernix import Cookie, Session
from pernix.session import SessionState

NOW = 1_800_000_000.0


def _session() -> Session:
    return Session(
        "s1",
        cookies=[Cookie(name="token", value="s3cr3t", domain="example.com", created=NOW, modified=NOW)],
        local_storage={"https://example.com": {"key": "st0red"}},
        proxy="http://user:pa55@proxy.example:8080",
        profile="chrome150",
        account="alice",
    )


def test_a_session_without_an_id_gets_a_random_one() -> None:
    first, second = Session(), Session()

    assert first.id != second.id
    assert len(first.id) == 32


def test_the_state_round_trips_through_msgspec() -> None:
    session = _session()

    state = msgspec.json.decode(msgspec.json.encode(session.state()), type=SessionState)
    restored = Session.from_state("s1", state)

    assert state == session.state()
    assert (restored.id, list(restored.cookies), restored.local_storage) == (
        "s1",
        list(session.cookies),
        {"https://example.com": {"key": "st0red"}},
    )
    assert (restored.proxy, restored.profile, restored.account) == (session.proxy, "chrome150", "alice")


def test_the_state_is_a_copy() -> None:
    session = _session()
    state = session.state()

    session.cookies.receive("https://example.com/", ["other=1"], now=NOW)
    session.local_storage["https://example.com"]["key"] = "changed"

    assert [cookie.name for cookie in state.cookies] == ["token"]
    assert state.local_storage == {"https://example.com": {"key": "st0red"}}


@pytest.mark.parametrize("described", [repr, str])
def test_cookies_storage_and_proxy_credentials_never_appear_in_a_repr(described: Callable[[object], str]) -> None:
    session = _session()

    for text in (described(session), described(session.state())):
        assert "s3cr3t" not in text
        assert "st0red" not in text
        assert "pa55" not in text
    assert repr(session) == (
        "Session('s1', cookies=1, local_storage=1 origins, proxy='http://***@proxy.example:8080', "
        "profile='chrome150', account='alice')"
    )


@pytest.mark.parametrize(
    ("proxy", "shown"),
    [
        ("alice:pa55@proxy.example:8080", "***@proxy.example:8080"),
        ("socks5://alice:pa55@proxy.example:1080", "socks5://***@proxy.example:1080"),
        ("http://proxy.example:8080", "http://proxy.example:8080"),
    ],
)
def test_proxy_credentials_are_masked_with_or_without_a_scheme(proxy: str, shown: str) -> None:
    session = Session("s", proxy=proxy)

    assert f"proxy={shown!r}" in repr(session)
    assert f"proxy={shown!r}" in repr(session.state())


def test_logging_a_session_shows_none_of_its_secrets(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        logging.getLogger("pernix.test").info("sent as %r", _session())

    assert "s3cr3t" not in caplog.text
    assert "pa55" not in caplog.text
