from pernix import Headers


def test_lookups_are_case_insensitive_and_keep_wire_order() -> None:
    headers = Headers([("Set-Cookie", "a=1"), ("Content-Type", "text/html"), ("set-cookie", "b=2")])

    assert headers.get("content-type") == "text/html"
    assert headers.get_all("SET-COOKIE") == ["a=1", "b=2"]
    assert "CONTENT-TYPE" in headers
    assert headers.items() == [("Set-Cookie", "a=1"), ("Content-Type", "text/html"), ("set-cookie", "b=2")]


def test_missing_headers_return_the_default() -> None:
    headers = Headers({"Accept": "*/*"})

    assert headers.get("Referer") is None
    assert headers.get("Referer", "-") == "-"
    assert headers.get_all("Referer") == []
    assert "Referer" not in headers


def test_headers_compare_by_value_and_order() -> None:
    assert Headers({"A": "1", "B": "2"}) == Headers([("A", "1"), ("B", "2")])
    assert Headers({"A": "1", "B": "2"}) != Headers([("B", "2"), ("A", "1")])
    assert Headers(Headers({"A": "1"})) == Headers({"A": "1"})
    assert len(Headers()) == 0
