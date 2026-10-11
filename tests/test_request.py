import pytest

from pernix import Request


def test_request_is_immutable_and_replaceable() -> None:
    request = Request("https://example.com/", headers={"A": "1"}, meta={"k": "v"})

    with pytest.raises(AttributeError):
        request.url = "https://example.com/other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.meta["k"] = "w"  # type: ignore[index]

    moved = request.replace(url="https://example.com/other", method="post")
    assert (moved.url, moved.method, moved.headers.get("a")) == (
        "https://example.com/other",
        "POST",
        "1",
    )
    assert request.url == "https://example.com/"


def test_request_encodes_str_bodies_as_utf8() -> None:
    assert Request("https://example.com/", method="POST", body="名字").body == "名字".encode()
