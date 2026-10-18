"""The default binding policy, and what a binding policy is told."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.proxies import Binding, DefaultBindingPolicy, Proxy, ProxyCandidate
from pernix.settings import ProxySettings

if TYPE_CHECKING:
    from pernix.proxies import BindingReason, ProxyHealth

A, B, C = "http://a.example:8080", "http://b.example:8080", "http://c.example:8080"


def _candidate(url: str, *sessions: str, health: ProxyHealth = "available") -> ProxyCandidate:
    return ProxyCandidate(url, health=health, sessions=frozenset(sessions))


def _binding(
    *candidates: ProxyCandidate,
    current: str | None = None,
    reason: BindingReason = "lease",
    max_sessions: int | None = None,
) -> Binding:
    return Binding("one", current=current, reason=reason, candidates=candidates, max_sessions=max_sessions)


async def _picked(binding: Binding, *, rebind: str = "unavailable") -> str | None:
    return await DefaultBindingPolicy(ProxySettings(rebind=rebind)).bind(binding)  # pyright: ignore[reportArgumentType]


async def test_a_session_keeps_its_proxy_while_it_is_open() -> None:
    binding = _binding(_candidate(A), _candidate(B, "one", "two"), current=B)

    assert await _picked(binding) == B


async def test_otherwise_it_is_bound_to_the_open_proxy_with_the_fewest_other_sessions() -> None:
    candidates = (
        _candidate(A, "two", "three"),
        _candidate(B, health="cooldown"),
        _candidate(C, "four"),
    )

    assert await _picked(_binding(*candidates, reason="new")) == C
    assert await _picked(_binding(*candidates, current=B, reason="cooldown")) == C


async def test_among_those_with_as_few_the_least_recently_used_goes_first() -> None:
    assert await _picked(_binding(_candidate(B, "two"), _candidate(C), _candidate(A), reason="new")) == C


async def test_a_session_whose_proxy_has_no_room_left_for_it_is_bound_to_another() -> None:
    binding = _binding(_candidate(A, "two"), _candidate(B), current=A, max_sessions=1)  # its state names A

    assert await _picked(binding) == B


async def test_with_every_lease_each_lease_binds_another_proxy_while_there_is_one() -> None:
    assert await _picked(_binding(_candidate(B), _candidate(A, "one"), current=A), rebind="every_lease") == B
    assert await _picked(_binding(_candidate(A, "one"), current=A), rebind="every_lease") == A


async def test_with_no_proxy_open_the_session_waits() -> None:
    binding = _binding(_candidate(A, "two"), _candidate(B, health="retired"), reason="new", max_sessions=1)

    assert await _picked(binding) is None


def test_sessions_leaving_a_proxy_take_its_room_until_their_leases_end() -> None:
    leaving = ProxyCandidate(A, sessions=frozenset({"two"}), leaving=frozenset({"three"}))

    assert leaving.occupants == {"two", "three"}
    assert _binding(leaving, max_sessions=2).open == ()
    assert _binding(leaving, max_sessions=3).open == (leaving,)


async def test_the_fewest_sessions_count_those_leaving() -> None:
    binding = _binding(ProxyCandidate(A, leaving=frozenset({"two"})), _candidate(B), reason="new")

    assert await _picked(binding) == B


def test_the_open_candidates_are_those_available_with_room_for_the_session() -> None:
    binding = _binding(
        _candidate(A, "one", "two"),  # its own, with room for it
        _candidate(B, "two", "three"),
        _candidate(C, health="cooldown"),
        max_sessions=2,
    )

    assert [candidate.url for candidate in binding.open] == [A]
    assert [candidate.url for candidate in _binding(*binding.candidates).open] == [A, B]


@pytest.mark.parametrize(
    ("record", "shown"),
    [
        (Proxy("http://user:pw@gate.example:7777", tags=frozenset({"us"})), "Proxy('http://***@gate.example:7777'"),
        (ProxyCandidate("http://user:pw@gate.example:7777"), "ProxyCandidate('http://***@gate.example:7777'"),
        (Binding("one", current="http://user:pw@gate.example:7777"), "current='http://***@gate.example:7777'"),
    ],
    ids=["proxy", "candidate", "binding"],
)
def test_records_show_their_proxies_without_credentials(record: object, shown: str) -> None:
    assert shown in repr(record)
    assert "pw" not in repr(record)


def test_tags_given_as_any_collection_are_held_as_a_frozenset() -> None:
    proxy = Proxy(A, tags=["us", "residential"])  # pyright: ignore[reportArgumentType]

    assert proxy.tags == frozenset({"us", "residential"})
    assert hash(proxy) == hash(Proxy(A, tags=frozenset({"residential", "us"})))
