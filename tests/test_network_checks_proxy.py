"""How the network checks show the proxy `--network-checks-proxy` names: without its credentials."""

from __future__ import annotations

from conftest import CheckProxy, _without_credentials  # pyright: ignore[reportPrivateUsage]
from pernix.fetchers.launched_browser import _proxy_settings  # pyright: ignore[reportPrivateUsage]

URL = "http://user:s3cr%40t@proxy.example:8080"


def test_the_proxy_of_the_network_checks_is_shown_without_its_credentials() -> None:
    proxy = CheckProxy(URL)

    assert (str(proxy), repr(proxy)) == ("http://***@proxy.example:8080", "CheckProxy('http://***@proxy.example:8080')")
    assert proxy.url == URL


def test_what_a_network_check_reports_holds_no_credentials_of_its_proxy() -> None:
    report = f"proxy = '{URL}'\nProxy-Authorization for s3cr@t, or s3cr%40t"

    assert _without_credentials(report, URL) == (
        "proxy = 'http://***@proxy.example:8080'\nProxy-Authorization for ***, or ***"
    )


def test_credentials_are_masked_as_a_traceback_shows_them_escaped() -> None:
    url = "http://user:s3cr%27%22t@proxy.example:8080"
    frame = f"proxy = {_proxy_settings(url)!r}, text = {'s3cr' + chr(39) + chr(34) + 't'!r}"

    shown = _without_credentials(frame, url)

    assert "s3cr" not in shown
    assert (
        shown == "proxy = {'server': 'http://proxy.example:8080', 'username': 'user', 'password': '***'}, text = '***'"
    )
