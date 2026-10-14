from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING

import orjson
import pytest

from loopback import Reply
from pernix import Request, Response, middleware
from pernix.cli import main
from pernix.cli.fetch_command import describe_response, is_text

if TYPE_CHECKING:
    from pathlib import Path

    from loopback import LoopbackServer, ReceivedRequest
    from pernix.middlewares import Download


@middleware
async def stamp(request: Request, call_next: Download) -> Response:
    """A download Middleware the tests register with -s."""
    return await call_next(request.replace(headers=[("X-Stamp", "cli")]))


@pytest.fixture
def site(server: LoopbackServer) -> LoopbackServer:
    server.route("/page", "<h1>Dune</h1>", headers=[("Content-Type", "text/html; charset=utf-8"), ("X-Tag", "a")])
    server.route("/old", status=301, headers=[("Location", "/page")])
    server.route("/logo", b"\x89PNG\x00\x01", headers=[("Content-Type", "image/png")])

    def slow(_request: ReceivedRequest) -> Reply:
        time.sleep(1)
        return Reply("late")

    server.route("/slow", handler=slow)
    return server


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_body_goes_to_stdout_and_the_rest_to_stderr(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["fetch", site.url("/old")])

    out, err = capsys.readouterr()
    assert code == 0
    assert out == "<h1>Dune</h1>"
    lines = err.splitlines()
    assert lines[0] == f"301 {site.url('/old')}"
    assert lines[1].startswith(f"HTTP/1.1 200 {site.url('/page')} (")
    assert lines[1].endswith(" s, 13 bytes)")
    assert "x-tag: a" in [line.lower() for line in lines[2:]]


def test_json_describes_the_response(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["fetch", site.url("/old"), "--json"])

    described = orjson.loads(capsys.readouterr().out)
    assert code == 0
    assert set(described) == {
        "url",
        "final_url",
        "status",
        "http_version",
        "headers",
        "encoding",
        "elapsed",
        "redirects",
        "fetcher",
        "body",
        "body_bytes",
    }
    assert (described["url"], described["final_url"]) == (site.url("/old"), site.url("/page"))
    assert (described["status"], described["http_version"], described["fetcher"]) == (200, "HTTP/1.1", "http")
    assert ["X-Tag", "a"] in described["headers"] or ["x-tag", "a"] in described["headers"]
    assert described["encoding"] == "utf-8"
    assert described["redirects"] == [{"url": site.url("/old"), "status": 301}]
    assert (described["body"], described["body_bytes"]) == ("<h1>Dune</h1>", 13)
    assert described["elapsed"] > 0


def test_the_body_can_be_saved_byte_for_byte(
    site: LoopbackServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "logo.png"

    assert main(["fetch", site.url("/logo"), "-o", str(target), "--json"]) == 0

    assert target.read_bytes() == b"\x89PNG\x00\x01"
    described = orjson.loads(capsys.readouterr().out)
    assert (described["body"], described["body_bytes"], described["encoding"]) == (None, 6, None)


def test_a_binary_body_is_written_raw_but_never_to_a_terminal(
    site: LoopbackServer, capsysbinary: pytest.CaptureFixture[bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["fetch", site.url("/logo")]) == 0
    assert capsysbinary.readouterr().out == b"\x89PNG\x00\x01"

    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert main(["fetch", site.url("/logo")]) == 0
    out, err = capsysbinary.readouterr()
    assert out == b""
    assert b"The body is binary; save it with -o FILE" in err


def test_a_failed_fetch_exits_with_1(capsys: pytest.CaptureFixture[str]) -> None:
    url = f"http://127.0.0.1:{_unused_port()}/"

    assert main(["fetch", url]) == 1
    assert capsys.readouterr().err.startswith(f"Error: fetching {url} failed: ConnectError: ")

    assert main(["fetch", url, "--json"]) == 1
    described = orjson.loads(capsys.readouterr().out)
    assert described["url"] == url
    assert (described["error"]["type"], described["error"]["code"]) == ("ConnectError", 7)


def test_the_timeout_limits_the_fetch(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    started = time.monotonic()

    assert main(["fetch", site.url("/slow"), "--timeout", "0.2", "--json"]) == 1

    assert time.monotonic() - started < 1
    assert orjson.loads(capsys.readouterr().out)["error"]["type"] == "Timeout"


def test_the_project_settings_and_middleware_apply(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["fetch", site.url("/page"), "-s", 'middlewares={"test_cli_fetch.stamp": 100}'])

    assert code == 0
    assert site.received[-1].header("X-Stamp") == "cli"
    assert capsys.readouterr().out == "<h1>Dune</h1>"


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--fetcher", "browser"], "no Fetcher named 'browser' is registered; registered: 'http'"),
        (["-s", "concurency=2"], "unknown setting 'concurency' from -s concurency; did you mean 'concurrency'?"),
    ],
)
def test_configuration_errors_exit_with_2(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str], argv: list[str], problem: str
) -> None:
    assert main(["fetch", site.url("/page"), *argv]) == 2

    assert problem in capsys.readouterr().err
    assert site.received == []


def test_a_relative_url_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", "/page"]) == 2
    assert capsys.readouterr().err.startswith("Error: ")


@pytest.mark.parametrize(
    ("content_type", "body", "text"),
    [
        ("text/plain", b"a", True),
        ("application/json; charset=utf-8", b"{}", True),
        ("application/ld+json", b"{}", True),
        ("image/svg+xml", b"<svg/>", True),
        ("application/octet-stream", b"a", False),
        (None, b"plain", True),
        (None, b"\x00\x01", False),
    ],
)
def test_text_bodies_are_told_by_media_type_or_content(content_type: str | None, body: bytes, text: bool) -> None:
    headers = [("Content-Type", content_type)] if content_type else []
    response = Response(
        url="https://example.com/", status=200, headers=headers, body=body, request=Request("https://example.com/")
    )

    assert is_text(response) is text
    assert (describe_response(response)["body"] is not None) is text
    assert describe_response(response, include_body=False)["body"] is None
