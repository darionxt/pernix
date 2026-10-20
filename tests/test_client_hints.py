"""Client hints: reading Accept-CH as Chrome does, keeping what origins ask for, and the values Chrome sends."""

from __future__ import annotations

import pytest

from pernix import Session
from pernix.fetchers import client_hints
from pernix.fetchers.chain_hints import ChainHints
from pernix.fetchers.client_hints import HintValues, device_memory, keep_accepted, parse_hints, sent_hints

SHOP = "https://shop.example"


@pytest.mark.parametrize(
    ("value", "hints"),
    [
        ("Sec-CH-UA-Arch, sec-ch-ua-model", ("sec-ch-ua-arch", "sec-ch-ua-model")),
        ("sec-ch-ua-model,sec-ch-ua-arch", ("sec-ch-ua-arch", "sec-ch-ua-model")),
        ("Sec-CH-UA-Arch;a=1;b, X-Nothing, DPR", ("dpr", "sec-ch-ua-arch")),
        ("  sec-ch-ua-arch\t,  sec-ch-ua-arch  ", ("sec-ch-ua-arch",)),
        ("", ()),
        ("X-Nothing", ()),
        ('"sec-ch-ua-arch"', None),
        ("sec-ch-ua-arch, (dpr)", None),
        ("sec-ch-ua-arch,", None),
        ("sec-ch-ua-arch;=1", None),
        ("1, sec-ch-ua-arch", None),
        (None, None),
    ],
)
def test_accept_ch_is_read_as_chrome_reads_a_list_of_tokens(value: str | None, hints: tuple[str, ...] | None) -> None:
    assert parse_hints(value) == hints


def test_the_hints_sent_are_those_chrome_adds_to_a_navigation_in_its_order() -> None:
    assert sent_hints(["sec-ch-ua-model", "width", "save-data", "sec-ch-width", "rtt", "sec-ch-ua-arch"]) == (
        "rtt",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-arch",
        "sec-ch-ua-platform",
        "sec-ch-ua-model",
    )
    assert sent_hints([]) == ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")


def test_an_origin_asking_again_replaces_what_it_asked_for_and_an_empty_list_forgets_it() -> None:
    asked: dict[str, tuple[str, ...]] = {}

    assert keep_accepted(asked, f"{SHOP}/a?b", "sec-ch-ua-arch")
    assert keep_accepted(asked, f"{SHOP}:443/c", "sec-ch-ua-model")
    assert asked == {SHOP: ("sec-ch-ua-model",)}
    assert not keep_accepted(asked, f"{SHOP}/d", "Sec-CH-UA-Model")
    assert not keep_accepted(asked, f"{SHOP}/e", None)
    assert not keep_accepted(asked, f"{SHOP}/f", '"broken"')
    assert keep_accepted(asked, f"{SHOP}/g", "X-Nothing")
    assert asked == {}


@pytest.mark.parametrize(
    ("url", "kept"),
    [("http://shop.example/", False), ("http://localhost:8080/", True), ("http://127.0.0.1/", True)],
)
def test_only_potentially_trustworthy_origins_ask_for_hints(url: str, kept: bool) -> None:
    asked: dict[str, tuple[str, ...]] = {}

    keep_accepted(asked, url, "sec-ch-ua-arch")

    assert bool(asked) == kept


def test_a_chain_keeps_what_its_responses_ask_for_in_its_session_too() -> None:
    session = Session(client_hints={SHOP: ("sec-ch-ua-arch",)})
    chain = ChainHints(session)

    missing = chain.receive(f"{SHOP}/", "sec-ch-ua-arch, sec-ch-ua-model", "sec-ch-ua-model, sec-ch-ua-arch, dpr")

    assert missing == ("sec-ch-ua-model",)  # its origin asked for the architecture before, and no DPR now
    assert chain.asked(f"{SHOP}/x") == session.client_hints[SHOP] == ("sec-ch-ua-arch", "sec-ch-ua-model")


def test_a_chain_without_a_session_keeps_its_hints_to_itself() -> None:
    chain = ChainHints(None)

    assert chain.receive(f"{SHOP}/", "sec-ch-ua-arch", None) == ()
    assert chain.asked(f"{SHOP}/") == ("sec-ch-ua-arch",)
    assert chain.asked("https://other.example/") == ()


@pytest.mark.parametrize(
    ("accept_ch", "critical_ch"),
    [(None, "sec-ch-ua-arch"), ("sec-ch-ua-model", "sec-ch-ua-arch"), ('"x"', "sec-ch-ua-arch")],
    ids=["no Accept-CH", "not accepted", "Accept-CH no list"],
)
def test_critical_ch_needs_only_what_accept_ch_asks_for(accept_ch: str | None, critical_ch: str) -> None:
    assert ChainHints(None).receive(f"{SHOP}/", accept_ch, critical_ch) == ()


def test_an_insecure_origin_asks_for_nothing() -> None:
    session = Session()

    assert ChainHints(session).receive("http://shop.example/", "sec-ch-ua-arch", "sec-ch-ua-arch") == ()
    assert session.client_hints == {}


@pytest.mark.parametrize(("platform", "version"), [("macOS", '"10_15_7"'), ("Linux", '""'), ("Windows", '"10.0"')])
def test_the_values_are_those_the_browsers_pages_present(platform: str, version: str) -> None:
    values = HintValues(platform, salt=1).values("shop.example")

    assert values["sec-ch-ua-platform-version"] == version
    assert (values["sec-ch-ua-arch"], values["sec-ch-ua-bitness"], values["sec-ch-ua-model"]) == ('"x86"', '"64"', '""')
    assert values["sec-ch-ua-full-version-list"] == '"Chromium";v="151.0.7922.34", "Not=A?Brand";v="99.0.0.0"'
    assert (values["sec-ch-viewport-width"], values["sec-ch-viewport-height"], values["sec-ch-dpr"]) == (
        "1280",
        "720",
        "1",
    )
    assert values["sec-ch-device-memory"] == values["device-memory"] == f"{device_memory():g}"


def test_the_network_hints_tell_of_a_fast_connection_with_chromes_noise_for_each_host() -> None:
    downlinks = {HintValues("Linux", salt=salt).values("shop.example")["downlink"] for salt in range(1, 22)}
    values = HintValues("Linux", salt=5)

    assert {HintValues("Linux", salt=salt).values("shop.example")["rtt"] for salt in range(1, 22)} == {"50"}
    assert downlinks == {"9", "9.1", "9.2", "9.3", "9.4", "9.5", "9.6", "9.7", "9.8", "9.9", "10"}
    assert values.values("shop.example") == values.values("shop.example")
    assert values.values("shop.example")["ect"] == "4g"


@pytest.mark.parametrize(
    ("megabytes", "gigabytes"),
    [(1024, 2.0), (3072, 2.0), (3073, 4.0), (7934, 8.0), (16384, 16.0), (65536, 32.0)],
)
def test_device_memory_is_rounded_as_chrome_rounds_it(
    monkeypatch: pytest.MonkeyPatch, megabytes: int, gigabytes: float
) -> None:
    monkeypatch.setattr(client_hints, "_physical_memory", lambda: megabytes * 1024 * 1024)
    device_memory.cache_clear()
    try:
        assert device_memory() == gigabytes
    finally:
        device_memory.cache_clear()
