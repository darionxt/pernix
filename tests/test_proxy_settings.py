"""The `proxies.*` settings."""

from __future__ import annotations

import pytest

from pernix.errors import SettingsError
from pernix.proxies import JsonAnswer, Proxy
from pernix.secret import Secret
from pernix.settings import ProxySettings, load_settings


def test_proxies_are_off_until_a_source_or_a_binding_policy_is_set() -> None:
    assert not ProxySettings().enabled
    assert ProxySettings(urls=("proxy.example:8080",)).enabled
    assert ProxySettings(api=Secret("https://vendor.example/proxies?key=k")).enabled
    assert ProxySettings(binding="myproject.proxies.Sticky").enabled  # giving Sessions proxies of their own
    assert not ProxySettings(binding="myproject.proxies.Sticky").sourced


def test_the_api_is_a_secret_as_its_url_often_holds_a_key() -> None:
    settings = load_settings(spider_settings={"proxies.api": "https://vendor.example/proxies?key=k"})

    assert settings.section(ProxySettings).api == Secret("https://vendor.example/proxies?key=k")


def test_listed_proxies_that_are_not_valid_are_named_without_quoting_them() -> None:
    with pytest.raises(SettingsError) as refused:
        load_settings(spider_settings={"proxies.urls": ["proxy.example:8080", "user:secret-pw@proxy.example"]})

    assert "proxies.urls[1]: a proxy needs a port" in str(refused.value)
    assert "secret-pw" not in str(refused.value)


def test_listed_proxies_may_be_tables_to_tag_them() -> None:
    settings = load_settings(
        spider_settings={"proxies.urls": ["a.example:1", {"url": "b.example:2", "tags": ["us", "residential"]}]}
    )

    assert settings.section(ProxySettings).urls == (
        "a.example:1",
        Proxy("b.example:2", tags=frozenset({"us", "residential"})),
    )


def test_a_listed_table_must_be_a_proxy_record() -> None:
    with pytest.raises(SettingsError, match="unknown field `tag`"):
        load_settings(spider_settings={"proxies.urls": [{"url": "b.example:2", "tag": ["us"]}]})
    with pytest.raises(SettingsError) as refused:
        load_settings(spider_settings={"proxies.urls": [{"url": "user:secret-pw@b.example"}]})

    assert "proxies.urls[0]: a proxy needs a port" in str(refused.value)
    assert "secret-pw" not in str(refused.value)


def test_sessions_keep_their_proxy_with_no_limit_on_how_many_share_one_unless_set() -> None:
    defaults = ProxySettings()

    assert (defaults.max_sessions, defaults.rebind, defaults.binding) == (None, "unavailable", None)
    for rebind in ("every_lease", "retired", "never"):
        assert load_settings(spider_settings={"proxies.rebind": rebind}).section(ProxySettings).rebind == rebind
    for wrong in ({"proxies.max_sessions": 0}, {"proxies.rebind": "always"}):
        with pytest.raises(SettingsError):
            load_settings(spider_settings=wrong)


def test_proxies_are_replaced_at_each_load_and_not_extracted_on_demand_unless_set() -> None:
    defaults = ProxySettings()

    assert (defaults.supply, defaults.api_lifetime, defaults.min_available, defaults.max_extracted) == (
        "replace",
        None,
        None,
        None,
    )
    assert (defaults.extract_interval, defaults.extract_attempts) == (1.0, 3)
    settings = load_settings(
        spider_settings={
            "proxies.api": "https://vendor.example/proxies?num={count}",
            "proxies.supply": "add",
            "proxies.api_lifetime": 300,
            "proxies.min_available": 10,
            "proxies.max_extracted": 1000,
        }
    ).section(ProxySettings)
    assert (settings.supply, settings.api_lifetime, settings.min_available, settings.max_extracted) == (
        "add",
        300,
        10,
        1000,
    )


@pytest.mark.parametrize(
    ("given", "problem"),
    [
        ({"proxies.api": "https://vendor.example/", "proxies.min_available": 1}, "proxies.min_available: proxies are"),
        ({"proxies.api": "https://vendor.example/", "proxies.max_extracted": 1}, "proxies.max_extracted: proxies are"),
        ({"proxies.urls": ["a.example:1"], "proxies.supply": "add", "proxies.min_available": 1}, "neither is set"),
        ({"proxies.urls": ["a.example:1"], "proxies.api_lifetime": 60}, "proxies.api_lifetime: it is the lifetime"),
    ],
    ids=["level without add", "limit without add", "level without an extracting source", "lifetime without api"],
)
def test_extraction_settings_that_cannot_apply_are_refused(given: dict[str, object], problem: str) -> None:
    with pytest.raises(SettingsError, match=problem):
        load_settings(spider_settings=given)


def test_the_api_is_read_one_proxy_per_line_unless_a_json_table_maps_its_answers() -> None:
    assert ProxySettings().api_json is None
    settings = load_settings(
        spider_settings={
            "proxies.api": "https://vendor.example/get?num={count}",
            "proxies.api_json": {"list": "data", "host": "ip", "port": "port"},
        }
    ).section(ProxySettings)

    assert settings.api_json == JsonAnswer(list="data", host="ip", port="port")
    with pytest.raises(SettingsError, match=r"proxies\.api_json: it tells how to read the answers of proxies\.api"):
        load_settings(spider_settings={"proxies.urls": ["a.example:1"], "proxies.api_json": {"list": "data"}})
    with pytest.raises(SettingsError, match=r"proxies\.api_json: host and port"):
        load_settings(spider_settings={"proxies.api": "https://vendor.example/", "proxies.api_json": {"host": "ip"}})
