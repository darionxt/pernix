"""The HTTP Fetcher keeping the client hints origins ask for, and sending them as Chrome does, against the loopback server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pernix import Request, Session
from pernix.fetchers import HTTPFetcher, HTTPSettings
from pernix.testing import LoopbackServer

if TYPE_CHECKING:
    from pernix.testing.loopback_server import ReceivedRequest

ARCH = ("Accept-CH", "Sec-CH-UA-Arch")


def _hints(received: ReceivedRequest) -> list[str]:
    """The client hints a request sent, but those every request sends."""
    defaults = {"sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
    names = [name.lower() for name, _ in received.headers]
    return [name for name in names if name.startswith("sec-ch-") and name not in defaults]


async def test_a_session_keeps_the_hints_an_origin_asks_for_and_sends_them_from_then_on(
    server: LoopbackServer,
) -> None:
    server.route("/asking", "", headers=[ARCH])
    server.route("/later", "")
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/asking")), session=session)
        await fetcher.fetch(Request(server.url("/later")), session=session)

    assert session.client_hints == {server.base_url: ("sec-ch-ua-arch",)}
    asking, later = server.received
    assert (_hints(asking), _hints(later)) == ([], ["sec-ch-ua-arch"])
    assert [name.lower() for name, _ in later.headers][2:6] == [
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-arch",
        "sec-ch-ua-platform",
    ]


async def test_without_a_session_the_hints_go_on_within_the_redirect_chain_only(server: LoopbackServer) -> None:
    server.route("/going", status=302, headers=[("Location", "/landing"), ARCH])
    server.route("/landing", "")

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/going")))
        await fetcher.fetch(Request(server.url("/landing")))

    going, landing, again = server.received
    assert (_hints(going), _hints(landing), _hints(again)) == ([], ["sec-ch-ua-arch"], [])
    names = [name for name, _ in landing.headers]
    assert names.index("sec-ch-ua-arch") == names.index("Sec-Fetch-Dest") + 1  # added as a redirect adds hints


async def test_hints_one_origin_asked_for_go_to_no_other(server: LoopbackServer) -> None:
    server.route("/asking", "", headers=[ARCH])
    session = Session()

    with LoopbackServer(routes={"/elsewhere": ""}) as other:
        async with HTTPFetcher() as fetcher:
            await fetcher.fetch(Request(server.url("/asking")), session=session)
            await fetcher.fetch(Request(other.url("/elsewhere")), session=session)
            await fetcher.fetch(
                Request(other.url("/elsewhere"), headers={"Sec-Fetch-Dest": "empty", "Referer": server.url("/page")}),
                session=session,
            )

    assert [_hints(received) for received in other.received] == [[], []]


async def test_a_page_sends_its_hints_to_its_own_origin(server: LoopbackServer) -> None:
    server.route("/asking", "", headers=[ARCH])
    server.route("/script.js", "")
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/asking")), session=session)
        await fetcher.fetch(
            Request(server.url("/script.js"), headers={"Sec-Fetch-Dest": "script", "Referer": server.url("/page")}),
            session=session,
        )

    assert _hints(server.received[-1]) == ["sec-ch-ua-arch"]


async def test_critical_ch_sends_the_navigation_again_from_the_requests_url(server: LoopbackServer) -> None:
    server.route("/going", status=302, headers=[("Location", "/critical")])
    critical = [ARCH, ("Critical-CH", "Sec-CH-UA-Arch"), ("Set-Cookie", "seen=1")]
    server.route("/critical", "page", headers=critical)

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/going")))

    assert [received.path for received in server.received] == ["/going", "/critical", "/going", "/critical"]
    first, _, again, last = server.received
    assert (_hints(first), _hints(again), _hints(last)) == ([], ["sec-ch-ua-arch"], ["sec-ch-ua-arch"])
    names = [name for name, _ in again.headers]
    assert names.index("sec-ch-ua") == names.index("Accept") + 1  # every hint after Accept, as Chrome sends it again
    assert last.header("Cookie") == "seen=1"
    assert [redirect.url for redirect in response.history] == [server.url("/going")]
    assert (response.text, response.cookies) == ("page", {"seen": "1"})


async def test_critical_ch_sends_a_navigation_again_once_for_each_origin(server: LoopbackServer) -> None:
    asking = [("Location", "/b"), ARCH, ("Critical-CH", "Sec-CH-UA-Arch")]
    server.route("/a", status=302, headers=asking)
    server.route("/b", "", headers=[("Accept-CH", "Sec-CH-UA-Model"), ("Critical-CH", "Sec-CH-UA-Model")])

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/a")))

    assert [received.path for received in server.received] == ["/a", "/a", "/b"]


async def test_critical_ch_needing_no_hint_more_sends_nothing_again(server: LoopbackServer) -> None:
    server.route("/critical", "", headers=[("Accept-CH", "Sec-CH-Width"), ("Critical-CH", "Sec-CH-Width")])
    server.route("/known", "", headers=[ARCH, ("Critical-CH", "Sec-CH-UA-Arch")])
    session = Session(client_hints={server.base_url: ("sec-ch-ua-arch",)})

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/critical")))  # Chrome adds no width to a navigation
        await fetcher.fetch(Request(server.url("/known")), session=session)

    assert [received.path for received in server.received] == ["/critical", "/known"]


async def test_a_form_submission_keeps_the_hints_it_is_asked_for_but_is_not_sent_again(
    server: LoopbackServer,
) -> None:
    server.route("/submit", "", method="POST", headers=[ARCH, ("Critical-CH", "Sec-CH-UA-Arch")])
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/submit"), data={"a": "1"}), session=session)

    assert len(server.received) == 1
    assert session.client_hints == {server.base_url: ("sec-ch-ua-arch",)}


async def test_the_responses_of_other_kinds_ask_for_no_hints(server: LoopbackServer) -> None:
    server.route("/api", "{}", headers=[ARCH])
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/api"), headers={"Sec-Fetch-Dest": "empty"}), session=session)
        await fetcher.fetch(Request(server.url("/frame"), headers={"Sec-Fetch-Dest": "iframe"}), session=session)

    assert session.client_hints == {}


async def test_another_profile_keeps_the_hints_asked_for_but_sends_none(server: LoopbackServer) -> None:
    server.route("/critical", "", headers=[ARCH, ("Critical-CH", "Sec-CH-UA-Arch")])
    session = Session()

    async with HTTPFetcher(HTTPSettings(impersonate="chrome146")) as fetcher:
        await fetcher.fetch(Request(server.url("/critical")), session=session)
        await fetcher.fetch(Request(server.url("/critical")), session=session)

    assert session.client_hints == {server.base_url: ("sec-ch-ua-arch",)}
    assert [_hints(received) for received in server.received] == [[], []]


async def test_an_empty_accept_ch_forgets_the_hints_and_one_that_is_no_list_changes_nothing(
    server: LoopbackServer,
) -> None:
    server.route("/forgetting", "", headers=[("Accept-CH", "")])
    server.route("/broken", "", headers=[("Accept-CH", '"Sec-CH-UA-Model"')])
    session = Session(client_hints={server.base_url: ("sec-ch-ua-arch",)})

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/broken")), session=session)
        kept = dict(session.client_hints)
        await fetcher.fetch(Request(server.url("/forgetting")), session=session)

    assert kept == {server.base_url: ("sec-ch-ua-arch",)}
    assert session.client_hints == {}
