"""How `pernix fetch` reports each URL: one JSON object per line, or the Response laid out for people."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import orjson

from pernix.cli.closed_stdout import discard_stdout
from pernix.cli.page_reading import ReadingOptions, is_text, read
from pernix.errors import RetriesExhausted

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.cli.page_reading import Reading
    from pernix.events import RequestDropped, RequestIgnored
    from pernix.response import Response

__all__ = ["SCHEMA", "FetchOutput", "describe_error", "describe_response"]

SCHEMA = 1
"""The version of the objects `pernix fetch --json` writes, raised when a field changes meaning or goes away."""


class FetchOutput:
    """Reports what `pernix fetch` got for each URL, as it gets it.

    With `json`, each URL is one JSON object on a line of stdout: its Response, as
    `describe_response()` describes it, or its error, as `describe_error()` does.
    Otherwise, the status line, the redirects, and the headers go to stderr, and
    what was read to stdout, or the error to stderr. Once stdout is closed, as when
    a reader such as `head` has read enough, nothing more is written.

    Attributes:
        failures: The URLs that got no Response.
        closed: Whether stdout was closed.
    """

    def __init__(self, options: ReadingOptions, *, json: bool, save_to: Path | None = None) -> None:
        """Prepare the output.

        Args:
            options: What to read from each Response.
            json: Whether to write JSON objects.
            save_to: The file to write the body to, byte for byte, if any.
        """
        self._options = options
        self._json = json
        self._save_to = save_to
        self.failures = 0
        self.closed = False

    def response(self, response: Response) -> None:
        """Report a Response, whatever its status; one whose body cannot be saved or read is a failure.

        Args:
            response: The Response.
        """
        if self._save_to is not None:
            try:
                self._save_to.write_bytes(response.body)
            except OSError as exc:
                self._failure(
                    response.request.url, type(exc).__name__, f"cannot write the body to {self._save_to}: {exc}"
                )
                return
        try:
            reading = read(response, self._options, include_body=self._save_to is None)
        except RecursionError:
            self._failure(response.request.url, "RecursionError", "the document nests elements too deeply to read")
            return
        if self._json:
            self._write_json(describe_response(response, reading=reading))
        else:
            self._print(response, reading)

    def failed(self, url: str, error: Exception) -> None:
        """Report a URL whose fetch failed; once its retries ran out, the last Response, if any, is reported instead.

        Args:
            url: The URL.
            error: Why it failed.
        """
        if isinstance(error, RetriesExhausted) and error.response is not None:
            self.response(error.response)
            return
        described = describe_error(error)
        self._failure(url, described["type"], described["message"], described["code"])

    def ignored(self, event: RequestIgnored) -> None:
        """Report a Request a download Middleware or a Fetcher ignored.

        Args:
            event: The Event.
        """
        self._failure(event.request.url, "IgnoreRequest", event.reason)

    def dropped(self, event: RequestDropped) -> None:
        """Report a Request that did not enter the Frontier, as when a component vetoed it.

        Args:
            event: The Event.
        """
        self._failure(event.request.url, "RequestDropped", event.reason)

    def _failure(self, url: str, kind: str, message: str, code: int | None = None) -> None:
        self.failures += 1
        if self._json:
            self._write_json({"schema": SCHEMA, "url": url, "error": {"type": kind, "message": message, "code": code}})
        else:
            self._write(sys.stderr, f"Error: fetching {url} failed: {kind}: {message}\n")

    def _print(self, response: Response, reading: Reading) -> None:
        lines = [f"{redirect.status} {redirect.url}\n" for redirect in response.history]
        elapsed = f"{response.elapsed:.2f} s, " if response.elapsed is not None else ""
        lines.append(
            f"{response.http_version or 'HTTP'} {response.status} {response.url} ({elapsed}{len(response.body)} bytes)\n"
        )
        lines.extend(f"{name}: {value}\n" for name, value in response.headers.items())
        lines.append("\n")
        if self._save_to is not None:
            lines.append(f"Saved the body to {self._save_to}\n")
        self._write(sys.stderr, "".join(lines))
        if reading.links is not None:
            self._write(sys.stdout, "".join(f"{link.url}\t{link.text}\n" for link in reading.links))
        elif reading.matches is not None:
            self._write(sys.stdout, "".join(f"{match}\n" for match in reading.matches))
        elif reading.body is not None:
            extracted = self._options.extract is not None and not reading.body.endswith("\n")
            self._write(sys.stdout, reading.body + ("\n" if extracted else ""))
        elif self._save_to is None and not is_text(response):
            if sys.stdout.isatty():
                self._write(sys.stderr, "The body is binary; save it with -o FILE\n")
            else:
                self._write_bytes(response.body)
        if reading.truncated:
            self._write(sys.stderr, f"Cut at {self._options.max_chars} characters (--max-chars)\n")

    def _write_json(self, value: dict[str, Any]) -> None:
        self._write(sys.stdout, orjson.dumps(value).decode() + "\n")

    def _write(self, stream: Any, text: str) -> None:
        if self.closed:
            return
        try:
            stream.write(text)
            stream.flush()
        except BrokenPipeError:
            self._close()

    def _write_bytes(self, body: bytes) -> None:
        if self.closed:
            return
        try:
            sys.stdout.flush()
            sys.stdout.buffer.write(body)
            sys.stdout.flush()
        except BrokenPipeError:
            self._close()

    def _close(self) -> None:
        """Stop writing to the stdout a reader closed, sending what Python still flushes at exit nowhere."""
        self.closed = True
        discard_stdout()


def describe_response(
    response: Response,
    options: ReadingOptions | None = None,
    *,
    include_body: bool = True,
    reading: Reading | None = None,
) -> dict[str, Any]:
    """Describe a Response as the JSON object of `pernix fetch --json`.

    Args:
        response: The Response.
        options: What to read from it; the body as it is when `None`.
        include_body: Whether to read the body.
        reading: What was read from it already, in place of `options`.

    Returns:
        `schema`, then `url` (requested), `final_url`, `status`, `http_version`,
        `headers` as `[name, value]` pairs, `encoding`, `elapsed` in seconds,
        `redirects` as `{url, status}` objects, `fetcher`, `body` (the decoded
        text, or what `--extract` made of it; `None` for a binary body, one left
        out, and when selectors or links were read), `body_bytes`, `matches` or
        `links` (`{url, text}` objects) when asked for, and `truncated`.
    """
    if reading is None:
        reading = read(response, options or ReadingOptions(), include_body=include_body)
    text = is_text(response)
    described: dict[str, Any] = {
        "schema": SCHEMA,
        "url": response.request.url,
        "final_url": response.url,
        "status": response.status,
        "http_version": response.http_version,
        "headers": [[name, value] for name, value in response.headers.items()],
        "encoding": response.encoding if text else None,
        "elapsed": response.elapsed,
        "redirects": [{"url": redirect.url, "status": redirect.status} for redirect in response.history],
        "fetcher": response.fetcher,
        "body": reading.body,
        "body_bytes": len(response.body),
    }
    if reading.matches is not None:
        described["matches"] = reading.matches
    if reading.links is not None:
        described["links"] = [{"url": link.url, "text": link.text} for link in reading.links]
    described["truncated"] = reading.truncated
    return described


def describe_error(error: Exception) -> dict[str, Any]:
    """Describe why a fetch failed, as the `error` of `pernix fetch --json`.

    Once retries ran out because of an exception, it is that exception that is
    described.

    Args:
        error: The error.

    Returns:
        Its `type`, its `message`, and its `code`, the transport's error code, such
        as libcurl's, if it has one.
    """
    if isinstance(error, RetriesExhausted) and isinstance(error.__cause__, Exception):
        error = error.__cause__
    return {"type": type(error).__name__, "message": str(error), "code": getattr(error, "code", None)}
