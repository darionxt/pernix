"""`pernix fetch` with several URLs: one small Run, reported as JSON Lines, and the address guard."""

from __future__ import annotations

import io
import socket
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import orjson
import pytest

from pernix import IgnoreRequest, Request, Response, middleware, spider_middleware
from pernix.cli import main
from pernix.testing import Reply

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from pernix.middlewares import Download, HandleResponse
    from pernix.testing import LoopbackServer, ReceivedRequest

_SCRIPTED = "<h1 id=title>Served</h1><script>document.getElementById('title').textContent = 'Rendered'</script>"

OPENED: list[str] = []
"""What the project components below were opened or called for."""


@middleware
async def ignore_private(request: Request, call_next: Download) -> Response:
    """A download Middleware that ignores the Requests for `/private`."""
    if urlsplit(request.url).path == "/private":
        raise IgnoreRequest("private pages are not fetched")
    return await call_next(request)


@middleware
async def render_scripted(request: Request, call_next: Download) -> Response:
    """A download Middleware that renders the Requests for `/scripted`, as a Middleware may decide to."""
    if urlsplit(request.url).path == "/scripted":
        request = request.replace(render=True)
    return await call_next(request)


@spider_middleware
async def swallow(response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
    """A spider Middleware that keeps every Callback from running."""
    OPENED.append(f"swallow {response.url}")
    return
    yield


class Store:
    """A Pipeline that notes when it is opened."""

    async def open(self) -> None:
        OPENED.append("store")

    async def process(self, item: Any) -> Any:
        return item


@pytest.fixture
def site(server: LoopbackServer) -> LoopbackServer:
    server.route("/a", "<h1>A</h1>", headers=[("Content-Type", "text/html")])
    server.route("/b", "<h1>B</h1>", headers=[("Content-Type", "text/html")])
    server.route("/private", "<h1>Private</h1>", headers=[("Content-Type", "text/html")])
    server.route("/scripted", _SCRIPTED, headers=[("Content-Type", "text/html")])
    return server


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _rows(capsys: pytest.CaptureFixture[str]) -> dict[str, dict[str, Any]]:
    """The JSON Lines written, by URL, checking there is one per URL."""
    rows = [orjson.loads(line) for line in capsys.readouterr().out.splitlines()]
    by_url = {row["url"]: row for row in rows}
    assert len(by_url) == len(rows)
    return by_url


def test_each_url_is_one_json_line_and_a_failing_one_exits_with_1(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    refused = f"http://127.0.0.1:{_unused_port()}/"
    urls = [site.url("/a"), refused, site.url("/missing"), site.url("/b")]

    assert main(["fetch", *urls, "--json", "--css", "h1::text", "-s", "retry.times=0"]) == 1

    rows = _rows(capsys)
    assert set(rows) == set(urls)
    assert (rows[site.url("/a")]["matches"], rows[site.url("/b")]["matches"]) == (["A"], ["B"])
    assert rows[site.url("/missing")]["status"] == 404
    failure = rows[refused]
    assert set(failure) == {"schema", "url", "error"}
    assert (failure["schema"], failure["error"]["type"], failure["error"]["code"]) == (1, "ConnectError", 7)


def test_every_url_that_got_a_response_exits_with_0(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    urls = [site.url("/a"), site.url("/missing"), site.url("/a")]

    assert main(["fetch", *urls, "--json"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert sorted(orjson.loads(line)["status"] for line in lines) == [
        200,
        200,
        404,
    ]  # a URL given twice is fetched twice


def test_urls_are_read_from_a_file_and_from_stdin(
    site: LoopbackServer, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = tmp_path / "urls.txt"
    listed.write_text(f"# pages\n{site.url('/a')}\n\n  {site.url('/b')}  \n/relative\n")

    assert main(["fetch", "-i", str(listed), "--json"]) == 1
    rows = _rows(capsys)
    assert (rows[site.url("/a")]["status"], rows[site.url("/b")]["status"]) == (200, 200)
    assert rows["/relative"]["error"]["type"] == "ValueError"

    monkeypatch.setattr("sys.stdin", io.StringIO(f"{site.url('/b')}\n"))
    assert main(["fetch", site.url("/a"), "-i", "-", "--json"]) == 0
    assert set(_rows(capsys)) == {site.url("/a"), site.url("/b")}


def test_retries_apply_and_a_status_that_stays_retried_is_still_a_response(
    server: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts: list[int] = []
    lock = threading.Lock()

    def flaky(_request: ReceivedRequest) -> Reply:
        with lock:
            attempts.append(1)
            return Reply("busy", status=503) if len(attempts) == 1 else Reply("ok")

    server.route("/flaky", handler=flaky)
    server.route("/down", "down", status=503)

    code = main(["fetch", server.url("/flaky"), server.url("/down"), "--json", "-s", "retry.delay_scale=0"])

    rows = _rows(capsys)
    assert code == 0
    assert (rows[server.url("/flaky")]["status"], rows[server.url("/flaky")]["body"]) == (200, "ok")
    assert (rows[server.url("/down")]["status"], rows[server.url("/down")]["body"]) == (503, "down")
    assert len([request for request in server.received if request.path == "/down"]) == 4  # retry.times is 3


def test_ignored_requests_are_reported_as_failures(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    middlewares = ["-s", 'middlewares={"test_cli_fetch_batch.ignore_private": 100}']

    assert main(["fetch", site.url("/a"), site.url("/private"), "--json", *middlewares]) == 1

    rows = _rows(capsys)
    assert rows[site.url("/a")]["status"] == 200
    assert rows[site.url("/private")]["error"] == {
        "type": "IgnoreRequest",
        "message": "private pages are not fetched",
        "code": None,
    }


def test_the_projects_feeds_spider_middleware_and_pipelines_are_left_out(
    site: LoopbackServer, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pernix.toml").write_text(
        '[spider_middlewares]\n"test_cli_fetch_batch.swallow" = 100\n'
        '[pipelines]\n"test_cli_fetch_batch.Store" = 100\n'
        '[feeds."items.jsonl"]\n'
    )
    monkeypatch.chdir(tmp_path)
    OPENED.clear()

    assert main(["fetch", site.url("/a"), "--json"]) == 0

    assert _rows(capsys)[site.url("/a")]["status"] == 200
    assert OPENED == []
    assert not (tmp_path / "items.jsonl").exists()


@pytest.mark.parametrize("path", ["/a", "/down"])
def test_a_body_that_cannot_be_saved_is_a_failure(
    site: LoopbackServer, tmp_path: Path, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    site.route("/down", "down", status=503)
    target = tmp_path / "missing" / "page.html"

    assert main(["fetch", site.url(path), "-o", str(target), "--json", "-s", "retry.times=0"]) == 1

    error = orjson.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "FileNotFoundError"
    assert error["message"].startswith(f"cannot write the body to {target}: ")


def test_several_urls_need_json_and_o_takes_one(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", site.url("/a"), site.url("/b")]) == 2
    assert capsys.readouterr().err == "Error: several URLs, or -i, are reported as JSON Lines; add --json\n"

    assert main(["fetch", site.url("/a"), "-i", "-", "-o", "body.html", "--json"]) == 2
    assert capsys.readouterr().err == "Error: -o writes the body of one URL; give one URL without -i\n"

    assert main(["fetch"]) == 2
    assert capsys.readouterr().err == "Error: give a URL to fetch, or -i FILE\n"

    assert main(["fetch", "-i", "missing.txt", "--json"]) == 2
    assert capsys.readouterr().err.startswith("Error: cannot read the URLs of missing.txt: ")
    assert site.received == []


def test_a_configuration_error_stops_the_run(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str], without_patchright: None
) -> None:
    urls = [site.url("/a"), site.url("/b")]

    assert main(["fetch", *urls, "--fetcher", "browser", "--json"]) == 2

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Error: ")
    assert err.endswith("install it with: pip install 'pernix[browser]'\n")


def test_a_closed_stdout_stops_the_run(site: LoopbackServer, monkeypatch: pytest.MonkeyPatch) -> None:
    class Closed(io.StringIO):
        def write(self, _text: str) -> int:
            raise BrokenPipeError

    monkeypatch.setattr("sys.stdout", Closed())

    assert main(["fetch", site.url("/a"), site.url("/b"), site.url("/a"), "--json", "-s", "concurrency=1"]) == 1

    assert len(site.received) < 3


def test_with_the_address_guard_loopback_urls_and_redirects_to_them_are_refused(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    port = urlsplit(site.base_url).port
    site.route("/hop", status=301, headers=[("Location", site.url("/a"))])
    guarded = ["-s", "network.public_only=true", "-s", 'network.allow_hosts=["localhost"]']

    assert main(["fetch", site.url("/a"), f"http://localhost:{port}/hop", "--json", *guarded]) == 1

    rows = _rows(capsys)
    assert rows[site.url("/a")]["error"]["message"] == (
        f"network.public_only refused {site.url('/a')}: 127.0.0.1 is a loopback address"
    )
    assert rows[f"http://localhost:{port}/hop"]["error"]["message"] == (
        f"network.public_only refused the redirect from http://localhost:{port}/hop to {site.url('/a')}: "
        "127.0.0.1 is a loopback address"
    )
    assert [request.path for request in site.received] == ["/hop"]


def test_the_address_guard_refuses_to_render(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["fetch", site.url("/a"), "--fetcher", "browser", "--json", "-s", "network.public_only=true"]

    assert main(argv) == 1

    error = orjson.loads(capsys.readouterr().out)["error"]
    assert error["type"] == "NetworkBlocked"
    assert "the browser resolves hosts itself" in error["message"]
    assert site.received == []


@pytest.mark.usefixtures("browser_engine")
def test_several_rendered_urls_and_a_mix_of_rendered_and_http_ones_complete(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    rendered = [site.url("/scripted"), site.url("/a"), site.url("/scripted")]

    assert main(["fetch", *rendered, "--fetcher", "browser", "--json", "--css", "h1::text"]) == 0
    rows = [orjson.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted((row["url"], row["fetcher"], row["matches"][0]) for row in rows) == [
        (site.url("/a"), "browser", "A"),
        (site.url("/scripted"), "browser", "Rendered"),
        (site.url("/scripted"), "browser", "Rendered"),
    ]

    mixed = ["-s", 'middlewares={"test_cli_fetch_batch.render_scripted": 100}']
    assert (
        main(["fetch", site.url("/a"), site.url("/scripted"), site.url("/b"), "--json", "--css", "h1::text", *mixed])
        == 0
    )
    rows = [orjson.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert sorted((row["url"], row["fetcher"], row["matches"][0]) for row in rows) == [
        (site.url("/a"), "http", "A"),
        (site.url("/b"), "http", "B"),
        (site.url("/scripted"), "browser", "Rendered"),
    ]
