import msgspec
import pytest

from pernix import Cookie, CookieJar

NOW = 1_800_000_000.0
DAY = 24 * 3600


def _names(jar: CookieJar, url: str, *, now: float = NOW) -> list[str]:
    return [cookie.name for cookie in jar.matching(url, now=now)]


def _received(url: str, *headers: str, now: float = NOW) -> CookieJar:
    jar = CookieJar()
    jar.receive(url, headers, now=now)
    return jar


def test_a_cookie_without_a_domain_goes_to_its_host_only() -> None:
    jar = _received("https://www.example.com/", "a=1")

    [cookie] = jar
    assert (cookie.domain, cookie.host_only) == ("www.example.com", True)
    assert _names(jar, "https://www.example.com/") == ["a"]
    assert _names(jar, "https://sub.www.example.com/") == []
    assert _names(jar, "https://example.com/") == []


def test_a_domain_cookie_goes_to_the_hosts_under_its_domain() -> None:
    jar = _received("https://www.example.com/", "a=1; Domain=.Example.com")

    [cookie] = jar
    assert (cookie.domain, cookie.host_only) == ("example.com", False)
    assert _names(jar, "https://example.com/") == ["a"]
    assert _names(jar, "https://deep.www.example.com/") == ["a"]
    assert _names(jar, "https://notexample.com/") == []


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.example.com/", "other.com"),  # another host
        ("https://example.com/", "www.example.com"),  # a host under it
        ("https://www.example.co.uk/", "co.uk"),  # a public suffix
        ("https://user.github.io/", "github.io"),  # a public suffix in the private section
        ("https://app.localhost/", "localhost"),  # an unknown top-level domain counts as one too
        ("http://127.0.0.1/", "0.0.1"),
    ],
)
def test_a_domain_the_host_may_not_set_cookies_for_is_refused(url: str, domain: str) -> None:
    assert len(_received(url, f"a=1; Domain={domain}")) == 0


@pytest.mark.parametrize(("url", "domain"), [("https://co.uk/", "co.uk"), ("http://127.0.0.1/", "127.0.0.1")])
def test_a_domain_naming_a_public_suffix_or_ip_host_itself_gives_a_host_only_cookie(url: str, domain: str) -> None:
    [cookie] = _received(url, f"a=1; Domain={domain}")

    assert (cookie.domain, cookie.host_only) == (domain, True)


def test_international_domains_are_kept_in_ascii() -> None:
    jar = _received("https://www.bücher.example/", "a=1; Domain=Bücher.example", "b=2")

    assert {(cookie.name, cookie.domain) for cookie in jar} == {
        ("a", "xn--bcher-kva.example"),
        ("b", "www.xn--bcher-kva.example"),
    }
    assert _names(jar, "https://www.xn--bcher-kva.example/") == ["a", "b"]


@pytest.mark.parametrize(
    ("request_path", "header", "path"),
    [
        ("/docs/page", "a=1", "/docs"),
        ("/docs/", "a=1", "/docs"),
        ("/page", "a=1", "/"),
        ("", "a=1", "/"),
        ("/docs/page", "a=1; Path=/api", "/api"),
        ("/docs/page", "a=1; Path=api", "/docs"),  # not starting with /: the default path
    ],
)
def test_paths_default_to_the_directory_of_the_request(request_path: str, header: str, path: str) -> None:
    [cookie] = _received(f"https://example.com{request_path}", header)

    assert cookie.path == path


@pytest.mark.parametrize(
    ("path", "sent"),
    [("/docs", True), ("/docs/", True), ("/docs/page", True), ("/docsearch", False), ("/", False), ("/Docs", False)],
)
def test_a_cookie_goes_to_its_path_and_the_paths_under_it(path: str, sent: bool) -> None:
    jar = _received("https://example.com/", "a=1; Path=/docs")

    assert _names(jar, f"https://example.com{path}") == (["a"] if sent else [])


def test_same_named_cookies_on_different_paths_are_kept_apart() -> None:
    jar = _received("https://example.com/", "id=root; Path=/", "id=docs; Path=/docs", "id=api; Path=/docs/api")

    assert len(jar) == 3
    assert [cookie.value for cookie in jar.matching("https://example.com/docs/api/x", now=NOW)] == [
        "api",
        "docs",
        "root",
    ]
    assert [cookie.value for cookie in jar.matching("https://example.com/other", now=NOW)] == ["root"]


def test_host_only_and_domain_cookies_of_one_name_are_kept_apart() -> None:
    jar = _received("https://example.com/", "id=host", "id=domain; Domain=example.com")

    assert sorted(cookie.value for cookie in jar.matching("https://example.com/", now=NOW)) == ["domain", "host"]
    assert [cookie.value for cookie in jar.matching("https://www.example.com/", now=NOW)] == ["domain"]


def test_cookies_go_longer_paths_first_then_earliest_set_first() -> None:
    jar = CookieJar()
    jar.receive("https://example.com/", ["b=1", "a=1"], now=NOW)
    jar.receive("https://example.com/", ["deep=1; Path=/x/y", "c=1"], now=NOW + 1)
    jar.receive("https://example.com/", ["b=2"], now=NOW + 2)  # replacing b keeps its place

    assert _names(jar, "https://example.com/x/y/z", now=NOW + 3) == ["deep", "b", "a", "c"]


def test_a_replacing_cookie_keeps_its_creation_time() -> None:
    jar = _received("https://example.com/", "a=1")
    jar.receive("https://example.com/", ["a=2"], now=NOW + 5)

    [cookie] = jar
    assert (cookie.value, cookie.created, cookie.modified) == ("2", NOW, NOW + 5)


@pytest.mark.parametrize("url", ["https://example.com/", "wss://example.com/", "http://localhost/", "http://[::1]/"])
def test_secure_cookies_go_over_secure_connections(url: str) -> None:
    jar = _received("https://example.com/", "a=1; Secure; Domain=example.com")
    jar.receive("http://localhost/", ["a=1; Secure"], now=NOW)
    jar.receive("http://[::1]/", ["a=1; Secure"], now=NOW)

    assert _names(jar, url) == ["a"]


def test_secure_cookies_never_go_over_plain_http() -> None:
    jar = _received("https://example.com/", "a=1; Secure", "b=2")

    assert _names(jar, "http://example.com/") == ["b"]


def test_a_secure_cookie_from_plain_http_is_refused() -> None:
    assert len(_received("http://example.com/", "a=1; Secure")) == 0


def test_plain_http_can_replace_a_secure_cookie_once_it_expired() -> None:
    jar = _received("https://example.com/", "a=old; Secure; Max-Age=1")
    jar.receive("http://example.com/", ["a=new"], now=NOW + 2)

    assert [(cookie.value, cookie.secure) for cookie in jar] == [("new", False)]


def test_plain_http_cannot_replace_a_secure_cookie() -> None:
    jar = _received("https://example.com/", "a=secure; Secure")
    jar.receive("http://example.com/", ["a=insecure", "a=sub; Domain=example.com", "b=2"], now=NOW)

    assert sorted((cookie.name, cookie.value) for cookie in jar) == [("a", "secure"), ("b", "2")]


@pytest.mark.parametrize(
    ("header", "kept"),
    [
        ("__Secure-a=1; Secure", True),
        ("__Secure-a=1", False),
        ("__secure-a=1", False),  # prefixes match in any case
        ("__Host-a=1; Secure; Path=/", True),
        ("__Host-a=1; Secure", False),  # the default path does not do, though it is /
        ("__Host-a=1; Secure; Path=/docs; Path=/", True),
        ("__Host-a=1; Path=/", False),
        ("__Host-a=1; Secure; Path=/docs", False),
        ("__Host-a=1; Secure; Path=/; Domain=example.com", False),
    ],
)
def test_name_prefixes_are_enforced(header: str, kept: bool) -> None:
    assert len(_received("https://example.com/", header)) == int(kept)


def test_same_site_is_kept_and_none_needs_secure() -> None:
    jar = _received(
        "https://example.com/", "a=1; SameSite=Lax", "b=1; SameSite=None", "c=1; SameSite=None; Secure", "d=1"
    )

    assert {cookie.name: cookie.same_site for cookie in jar} == {"a": "lax", "c": "none", "d": None}


def test_max_age_and_expires_set_the_expiry() -> None:
    jar = _received(
        "https://example.com/",
        "session=1",
        "aged=1; Max-Age=60; Expires=Thu, 01 Jan 2099 00:00:00 GMT",
        "dated=1; Expires=Fri, 15 Jan 2027 09:00:00 GMT",  # an hour after NOW
    )

    assert {cookie.name: cookie.expires for cookie in jar} == {"session": None, "aged": NOW + 60, "dated": NOW + 3600}
    assert _names(jar, "https://example.com/", now=NOW + 61) == ["session", "dated"]


def test_expiry_is_capped_at_400_days() -> None:
    jar = _received("https://example.com/", "a=1; Max-Age=999999999", "b=1; Expires=Fri, 31 Dec 9999 23:59:59 GMT")

    assert {cookie.expires for cookie in jar} == {NOW + 400 * DAY}


@pytest.mark.parametrize(
    "header", ["a=; Max-Age=0", "a=gone; Max-Age=-1", "a=gone; Expires=Thu, 01 Jan 1970 00:00:00 GMT"]
)
def test_an_expired_cookie_removes_the_one_it_replaces(header: str) -> None:
    jar = _received("https://example.com/", "a=1", "b=1")
    jar.receive("https://example.com/", [header], now=NOW)

    assert [cookie.name for cookie in jar] == ["b"]


def test_expired_cookies_are_not_sent_until_cleared() -> None:
    jar = _received("https://example.com/", "a=1; Max-Age=10", "b=1")

    assert _names(jar, "https://example.com/", now=NOW + 20) == ["b"]
    assert len(jar) == 2
    jar.clear_expired(now=NOW + 20)
    assert [cookie.name for cookie in jar] == ["b"]


def test_cookies_larger_than_4096_bytes_are_refused() -> None:
    jar = _received("https://example.com/", f"a={'x' * 4096}", f"b={'x' * 4095}", f"é={'x' * 4095}")  # é is 2 bytes

    assert [cookie.name for cookie in jar] == ["b"]


@pytest.mark.parametrize(("header", "kept"), [("a=1\x01", False), ("a\x7f=1", False), ("a=1\t2", True)])
def test_control_characters_but_the_tab_are_refused(header: str, kept: bool) -> None:
    assert len(_received("https://example.com/", header)) == int(kept)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///tmp/x", "http:///path"])
def test_urls_without_a_cookie_host_neither_set_nor_get_cookies(url: str) -> None:
    jar = _received(url, "a=1")
    jar.set(Cookie(name="b", value="1", domain="example.com"))

    assert len(jar) == 1
    assert jar.matching(url, now=NOW) == []


def test_set_keeps_a_cookie_as_given_replacing_the_same_one() -> None:
    jar = CookieJar([Cookie(name="a", value="1", domain="example.com", created=NOW, modified=NOW)])
    jar.set(Cookie(name="a", value="2", domain="example.com", created=NOW + 1, modified=NOW + 1))
    jar.set(Cookie(name="a", value="3", domain="example.com", path="/docs", created=NOW, modified=NOW))

    assert [(cookie.value, cookie.path) for cookie in jar] == [("2", "/"), ("3", "/docs")]


def test_cookies_round_trip_through_msgspec() -> None:
    [cookie] = _received(
        "https://example.com/docs/", "a=1; Domain=example.com; Secure; HttpOnly; SameSite=Strict; Max-Age=60"
    )

    decoded = msgspec.json.decode(msgspec.json.encode(cookie), type=Cookie)

    assert decoded == cookie
    assert decoded == Cookie(
        name="a",
        value="1",
        domain="example.com",
        host_only=False,
        path="/docs",
        expires=NOW + 60,
        secure=True,
        http_only=True,
        same_site="strict",
        created=NOW,
        modified=NOW,
    )


def test_cookie_values_never_appear_in_a_repr() -> None:
    jar = _received("https://example.com/", "token=s3cr3t")

    assert "s3cr3t" not in repr(jar)
    assert "s3cr3t" not in repr(list(jar))
    assert repr(jar) == "CookieJar(1 cookies)"


def test_removals_tell_when_a_response_last_removed_a_cookie_until_it_is_set_again() -> None:
    jar = _received("https://example.com/", "a=1", "b=1")
    jar.receive("https://example.com/", ["a=; Max-Age=0", "never=; Max-Age=0"], now=NOW + 5)
    jar.receive("https://example.com/", ["a=; Max-Age=0"], now=NOW + 7)  # held no more, but removed again

    assert jar.removals == {("example.com", True, "/", "a"): NOW + 7, ("example.com", True, "/", "never"): NOW + 5}
    jar.receive("https://example.com/", ["a=2"], now=NOW + 8)
    assert jar.removals == {("example.com", True, "/", "never"): NOW + 5}
