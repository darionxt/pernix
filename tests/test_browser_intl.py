"""The browser's pages, their frames, and their web workers present the locale and the time zone of the settings."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import pytest

from pernix import Request, Session
from pernix.browser import Evaluate, Render
from pernix.fetchers import BrowserFetcher

if TYPE_CHECKING:
    from pernix.response import Response
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

LOCALE = "de-DE,de,en-US,en"
TIMEZONE = "Pacific/Chatham"  # unlike any machine's: UTC+12:45

INTL = """const intl = where => {
  const options = Intl.DateTimeFormat().resolvedOptions();
  return [where, options.locale, options.timeZone];
};
"""

WORKERS = f"""<!doctype html><body><script>{INTL}
const answer = (port, start = () => {{}}) => new Promise(resolve => {{
  port.onmessage = event => resolve(event.data);
  start();
}});
window.reports = (async () => {{
  const reports = [intl("page")];
  reports.push(await answer(new Worker("/dedicated.js")));
  reports.push(await answer(new Worker("/nesting.js")));
  const shared = new SharedWorker("/shared.js");
  reports.push(await answer(shared.port, () => shared.port.start()));
  await navigator.serviceWorker.register("/service.js");
  const registration = await navigator.serviceWorker.ready;
  const channel = new MessageChannel();
  reports.push(await answer(channel.port1, () => registration.active.postMessage(null, [channel.port2])));
  return reports;
}})();
</script></body>"""

SCRIPTS = {
    "/dedicated.js": f'{INTL}postMessage(intl("dedicated worker"));',
    "/nesting.js": (
        'const inner = new Worker("/dedicated.js");'
        'inner.onmessage = event => postMessage(["worker of a worker", ...event.data.slice(1)]);'
    ),
    "/shared.js": f'{INTL}onconnect = event => event.ports[0].postMessage(intl("shared worker"));',
    "/service.js": (
        f'{INTL}addEventListener("install", () => skipWaiting());'
        'addEventListener("activate", event => event.waitUntil(clients.claim()));'
        'addEventListener("message", event => event.ports[0].postMessage(intl("service worker")));'
    ),
}

FRAME = f"""<!doctype html><body><script>{INTL}
top.postMessage(intl(`frame of ${{location.hostname}}`), "*");
const worker = new Worker("/dedicated.js");
worker.onmessage = event => top.postMessage([`worker of ${{location.hostname}}`, ...event.data.slice(1)], "*");
const next = new URLSearchParams(location.search).get("next");
if (next) document.body.append(Object.assign(document.createElement("iframe"), {{src: next}}));
</script></body>"""

FRAMED = f"""<!doctype html><body><script>{INTL}
window.reports = new Promise(resolve => {{
  const reports = [intl("page")];
  addEventListener("message", event => {{
    reports.push(event.data);
    if (reports.length === 5) resolve(reports);
  }});
}});
</script><iframe src="FRAME"></iframe></body>"""

REPORTS = Render(actions=[Evaluate("() => window.reports")])

WORKERS_REPORTING = ("dedicated worker", "page", "service worker", "shared worker", "worker of a worker")


def _serve(server: LoopbackServer) -> None:
    html = [("Content-Type", "text/html")]
    server.route("/", WORKERS, headers=html)
    for path, script in SCRIPTS.items():
        server.route(path, script, headers=[("Content-Type", "text/javascript")])
    port = urlsplit(server.base_url).port
    inner = f"http://a.localhost:{port}/frame"
    server.route(
        "/framed", FRAMED.replace("FRAME", f"http://localhost:{port}/frame?next={quote(inner, safe='')}"), headers=html
    )
    server.route("/frame", FRAME, headers=html)
    server.route("/away", status=302, headers=[("Location", f"http://localhost:{port}/")])


def _reports(response: Response) -> list[list[str]]:
    assert response.browser is not None
    [reports] = response.browser.evaluated
    return sorted(reports)


@pytest.mark.parametrize("session", [None, Session("alice")], ids=["a context of its own", "a Session's context"])
async def test_a_page_and_its_web_workers_present_the_first_language_of_the_locale_and_the_time_zone(
    server: LoopbackServer, session: Session | None
) -> None:
    _serve(server)

    async with BrowserFetcher(locale=LOCALE, timezone=TIMEZONE) as fetcher:
        first = await fetcher.fetch(Request(server.url("/"), render=REPORTS), session=session)
        second = await fetcher.fetch(Request(server.url("/"), render=REPORTS), session=session)

    expected = [[where, "de-DE", TIMEZONE] for where in WORKERS_REPORTING]
    assert _reports(first) == _reports(second) == expected


async def test_cross_site_frames_at_any_depth_and_their_workers_present_them_too(server: LoopbackServer) -> None:
    _serve(server)

    async with BrowserFetcher(locale=LOCALE, timezone=TIMEZONE) as fetcher:
        response = await fetcher.fetch(Request(server.url("/framed"), render=REPORTS))

    where = ["frame of a.localhost", "frame of localhost", "page", "worker of a.localhost", "worker of localhost"]
    assert _reports(response) == [[place, "de-DE", TIMEZONE] for place in where]


async def test_a_page_redirected_to_another_site_presents_them(server: LoopbackServer) -> None:
    _serve(server)

    async with BrowserFetcher(locale=LOCALE, timezone=TIMEZONE) as fetcher:
        response = await fetcher.fetch(Request(server.url("/away"), render=REPORTS))

    assert response.url == f"http://localhost:{urlsplit(server.base_url).port}/"
    assert _reports(response) == [[where, "de-DE", TIMEZONE] for where in WORKERS_REPORTING]


async def test_by_default_the_intl_locale_is_american_english_and_the_time_zone_the_machines(
    server: LoopbackServer,
) -> None:
    server.route("/", "<p>page</p>", headers=[("Content-Type", "text/html")])
    render = Render(
        actions=[
            Evaluate("Intl.DateTimeFormat().resolvedOptions().locale"),
            Evaluate("-new Date().getTimezoneOffset()"),
        ]
    )

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/"), render=render))

    offset = dt.datetime.now().astimezone().utcoffset()
    assert offset is not None
    assert response.browser is not None
    assert response.browser.evaluated == ("en-US", offset // dt.timedelta(minutes=1))


def test_a_time_zone_the_system_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError, match=r"^no time zone is named '\+08:00' here; give an IANA name"):
        BrowserFetcher(timezone="+08:00")
