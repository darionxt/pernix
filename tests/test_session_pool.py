import msgspec

from pernix import Cookie
from pernix.session import SessionState
from pernix.sessions import CookieRemoval, StateDelta

NOW = 1_800_000_000.0


def _cookie(name: str, value: str, *, modified: float = NOW, path: str = "/") -> Cookie:
    return Cookie(name=name, value=value, domain="example.com", path=path, created=NOW, modified=modified)


def test_a_delta_holds_the_cookies_set_replaced_and_removed() -> None:
    kept, replaced, removed = _cookie("kept", "1"), _cookie("replaced", "1"), _cookie("removed", "1")
    before = SessionState(cookies=[kept, replaced, removed])
    after = SessionState(cookies=[kept, _cookie("replaced", "2", modified=NOW + 1), _cookie("new", "1")])

    delta = StateDelta.between(before, after, now=NOW + 2)

    assert [(cookie.name, cookie.value) for cookie in delta.cookies] == [("replaced", "2"), ("new", "1")]
    assert delta.removed == [
        CookieRemoval(name="removed", domain="example.com", host_only=True, path="/", removed=NOW + 2)
    ]
    assert not delta.empty


def test_a_delta_holds_the_origins_whose_storage_changed() -> None:
    before = SessionState(local_storage={"https://a.example": {"k": "1"}, "https://b.example": {"k": "1"}})
    after = SessionState(local_storage={"https://a.example": {"k": "2"}, "https://c.example": {"k": "1"}})

    delta = StateDelta.between(before, after, now=NOW)

    assert delta.local_storage == {
        "https://a.example": {"k": "2"},
        "https://c.example": {"k": "1"},
        "https://b.example": {},
    }


def test_a_delta_holds_the_origins_whose_client_hints_changed() -> None:
    before = SessionState(client_hints={"https://a.example": ("dpr",), "https://b.example": ("dpr",)})
    after = SessionState(client_hints={"https://a.example": ("sec-ch-ua-arch",), "https://b.example": ("dpr",)})

    delta = StateDelta.between(before, after, now=NOW)
    gone = StateDelta.between(after, SessionState(), now=NOW)

    assert delta.client_hints == {"https://a.example": ("sec-ch-ua-arch",)}
    assert gone.client_hints == {"https://a.example": (), "https://b.example": ()}
    assert not gone.empty
    assert msgspec.json.decode(msgspec.json.encode(delta), type=StateDelta) == delta


def test_an_unchanged_state_gives_an_empty_delta() -> None:
    state = SessionState(cookies=[_cookie("a", "1")], local_storage={"https://a.example": {"k": "1"}})

    assert StateDelta.between(state, state, now=NOW).empty


def test_deltas_round_trip_through_msgspec() -> None:
    delta = StateDelta.between(
        SessionState(cookies=[_cookie("gone", "1")]), SessionState(cookies=[_cookie("new", "1")]), now=NOW
    )

    assert msgspec.json.decode(msgspec.json.encode(delta), type=StateDelta) == delta


def test_a_delta_holds_every_removal_timed_even_of_cookies_the_fetch_started_without() -> None:
    key = ("example.com", True, "/", "sid")
    before = SessionState(cookies=[_cookie("kept", "1")])

    delta = StateDelta.between(before, before, now=NOW + 9, removed_at={key: NOW + 3})

    assert delta.removed == [CookieRemoval(name="sid", domain="example.com", host_only=True, path="/", removed=NOW + 3)]
