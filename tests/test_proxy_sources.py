"""The sources of proxies: a file, and an API answering with one per line."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.errors import FetchError, SettingsError
from pernix.fetchers import HTTPFetcher
from pernix.proxies import Proxy, read_proxies
from pernix.proxies.proxy_sources import ProxyAPI, ProxyFile, StaticProxies, proxy_lines
from pernix.secret import Secret

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.testing import LoopbackServer


def test_lines_skip_blanks_and_comments() -> None:
    text = "# vendor list\n\nproxy-a.example:1\n  proxy-b.example:2  \r\n#proxy-c.example:3\n"

    assert proxy_lines(text) == [(3, "proxy-a.example:1"), (4, "proxy-b.example:2")]


def test_a_file_gives_its_proxies_in_order(tmp_path: Path) -> None:
    file = tmp_path / "proxies.txt"
    file.write_text("﻿# ours\nhttp://user:pw@a.example:8080\nb.example:3128\n", encoding="utf-8")

    assert read_proxies(file) == ["http://user:pw@a.example:8080", "http://b.example:3128"]


def test_a_file_with_lines_that_are_not_proxies_names_each_without_quoting_it(tmp_path: Path) -> None:
    file = tmp_path / "proxies.txt"
    file.write_text("a.example:1\nuser:secret-pw@b.example\nftp://c.example:21\n")

    with pytest.raises(SettingsError) as refused:
        read_proxies(file)

    assert refused.value.problems == [
        f"{file}, line 2: a proxy needs a port",
        f"{file}, line 3: a proxy's scheme is one of http, https, socks4, socks4a, socks5, socks5h",
    ]
    assert "secret-pw" not in str(refused.value)


def test_a_missing_file_is_a_settings_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="cannot read the proxies"):
        read_proxies(tmp_path / "missing.txt")


async def test_a_file_is_read_anew_each_time(tmp_path: Path) -> None:
    file = tmp_path / "proxies.txt"
    source = ProxyFile(file)
    file.write_text("a.example:1\n")
    first = await source.proxies()
    file.write_text("b.example:2\n")

    assert (first, await source.proxies()) == (["http://a.example:1"], ["http://b.example:2"])


async def test_a_list_gives_its_proxies() -> None:
    assert await StaticProxies(["a.example:1"]).proxies() == ("a.example:1",)


async def test_an_api_gives_the_lines_it_answers_with(server: LoopbackServer) -> None:
    server.route("/proxies", "a.example:1\r\n# expires soon\r\nuser:pw@b.example:2\r\n\r\n")
    api = ProxyAPI(Secret(server.url("/proxies?key=k")), HTTPFetcher())

    await api.open()
    try:
        proxies = await api.proxies()
    finally:
        await api.close()

    assert proxies == ["a.example:1", "user:pw@b.example:2"]
    [received] = server.received
    assert received.query == [("key", "k")]


async def test_an_api_answering_with_an_error_fails_without_naming_its_url(server: LoopbackServer) -> None:
    server.route("/proxies", "busy", status=503)
    api = ProxyAPI(Secret(server.url("/proxies?key=k")), HTTPFetcher())

    await api.open()
    try:
        with pytest.raises(FetchError, match="answered with the status 503") as failed:
            await api.proxies()
    finally:
        await api.close()

    assert "key=k" not in str(failed.value)


async def test_an_api_is_told_how_many_proxies_are_wanted_and_its_url_as_fetched_is_masked(
    server: LoopbackServer,
) -> None:
    server.route("/proxies", "a.example:1\n")
    masked: list[str] = []
    api = ProxyAPI(Secret(server.url("/proxies?key=k&num={count}")), HTTPFetcher(), mask=masked.extend)

    await api.open()
    try:
        await api.proxies(wanted=5)
        await api.proxies()
    finally:
        await api.close()

    assert [received.query for received in server.received] == [
        [("key", "k"), ("num", "5")],
        [("key", "k"), ("num", "1")],
    ]
    assert masked == [server.url("/proxies?key=k&num=5"), server.url("/proxies?key=k&num=1")]


async def test_the_proxies_of_an_api_with_a_lifetime_expire_that_long_after_it_answered(server: LoopbackServer) -> None:
    server.route("/proxies", "a.example:1\nb.example:2:user:pw\n")
    api = ProxyAPI(Secret(server.url("/proxies")), HTTPFetcher(), lifetime=300, clock=lambda: 1_800_000_000.0)

    await api.open()
    try:
        proxies = await api.proxies()
    finally:
        await api.close()

    assert proxies == [
        Proxy("a.example:1", expires=1_800_000_300.0),
        Proxy("b.example:2:user:pw", expires=1_800_000_300.0),
    ]
