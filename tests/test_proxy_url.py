"""Proxy URLs: the one form each proxy is held in, and its credentials."""

from __future__ import annotations

import pytest

from pernix.proxies import proxy_url
from pernix.proxies.proxy_url import proxy_credentials


@pytest.mark.parametrize(
    ("given", "held"),
    [
        ("proxy.example:8080", "http://proxy.example:8080"),
        ("HTTP://Proxy.Example:8080", "http://proxy.example:8080"),
        ("http://proxy.example:8080/", "http://proxy.example:8080"),
        ("  socks5h://10.0.0.1:1080  ", "socks5h://10.0.0.1:1080"),
        ("https://[::1]:3128", "https://[::1]:3128"),
        ("User-Zone:Pass%40word@Gate.Example:7777", "http://User-Zone:Pass%40word@gate.example:7777"),
    ],
)
def test_a_proxy_is_held_with_its_scheme_and_port_its_scheme_and_host_lower_cased(given: str, held: str) -> None:
    assert proxy_url(given) == held


def test_sticky_sessions_on_one_gateway_stay_two_proxies() -> None:
    first = proxy_url("http://customer-session-a1:pw@gate.example:7777")
    second = proxy_url("http://customer-session-b2:pw@gate.example:7777")

    assert first != second


@pytest.mark.parametrize(
    ("given", "reason"),
    [
        ("proxy.example", "needs a port"),
        ("ftp://proxy.example:21", "scheme is one of"),
        ("http://:8080", "needs a host"),
        ("http://proxy.example:8080/path", "nothing after its port"),
        ("http://proxy.example:8080?key=1", "nothing after its port"),
        ("http://proxy.example:99999", "not a proxy URL"),
    ],
)
def test_what_is_not_a_proxy_is_refused_without_quoting_it(given: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason) as refused:
        proxy_url(given)

    assert "proxy.example" not in str(refused.value)


def test_a_proxys_password_is_masked_as_written_and_decoded() -> None:
    assert proxy_credentials("http://user:p%40ss@proxy.example:8080") == ("p%40ss", "p@ss")
    assert proxy_credentials("http://user:plain@proxy.example:8080") == ("plain",)
    assert proxy_credentials("http://proxy.example:8080") == ()
