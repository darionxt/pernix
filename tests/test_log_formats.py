from __future__ import annotations

import logging
import sys

import orjson

from pernix.logs import JsonFormatter, TextFormatter


def _record(message: str = "fetched %s", *args: object, **extra: object) -> logging.LogRecord:
    record = logging.makeLogRecord(
        {
            "name": "pernix.engine",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": message,
            "args": args,
            "created": 1_760_000_000.25,
            "msecs": 250.0,
        }
    )
    record.__dict__.update(extra)
    return record


def _failed() -> logging.LogRecord:
    try:
        raise ValueError("bad page")
    except ValueError:
        record = _record("parse failed")
        record.exc_info = sys.exc_info()
        return record


def test_json_lines_carry_the_run_the_request_and_the_extras() -> None:
    record = _record("fetched %s", "/a", run_id="r1", spider="books", request_id="17", page=3, tags={"x"})

    line = JsonFormatter().format(record)

    assert "\n" not in line
    assert orjson.loads(line) == {
        "time": "2025-10-09T08:53:20.250+00:00",
        "level": "INFO",
        "logger": "pernix.engine",
        "message": "fetched /a",
        "run_id": "r1",
        "spider": "books",
        "request_id": "17",
        "page": 3,
        "tags": "{'x'}",
    }


def test_json_lines_outside_a_run_have_null_ids_and_keep_their_own_fields() -> None:
    record = _record("hello", level="custom", huge=2**70)

    payload = orjson.loads(JsonFormatter().format(record))

    assert (payload["run_id"], payload["spider"], payload["request_id"]) == (None, None, None)
    assert payload["level"] == "INFO"  # extras never replace the standard fields
    assert list(payload) == ["time", "level", "logger", "message", "run_id", "spider", "request_id", "huge"]
    assert payload["huge"] == str(2**70)


def test_json_lines_include_the_exception_and_the_stack() -> None:
    record = _failed()
    record.stack_info = "Stack (most recent call last):\n  here"

    payload = orjson.loads(JsonFormatter().format(record))

    assert payload["exception"].startswith("Traceback (most recent call last):")
    assert payload["exception"].endswith("ValueError: bad page")
    assert payload["stack"] == "Stack (most recent call last):\n  here"


def test_text_lines_show_the_request_id_and_the_exception() -> None:
    line = TextFormatter().format(_record("fetched %s", "/a", request_id="17"))
    failed = TextFormatter().format(_failed())

    assert line.endswith(" INFO     [pernix.engine #17] fetched /a")
    assert line[:23].count(":") == 2  # a local time such as 2025-10-09 16:53:20.250
    assert line[19:23] == ".250"
    assert "[pernix.engine] parse failed\nTraceback (most recent call last):" in failed


def test_text_lines_list_the_final_stats_only() -> None:
    stats = {"items.scraped": 3, "run.reason": "finished"}

    final = TextFormatter().format(_record("Run finished", stats=stats, final=True))
    periodic = TextFormatter().format(_record("Fetched 3 responses", stats=stats, final=False))

    assert final.endswith("Run finished\n  items.scraped: 3\n  run.reason: finished")
    assert periodic.endswith("Fetched 3 responses")
