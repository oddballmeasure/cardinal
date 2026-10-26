"""POST /v1/records: one NDJSON line that is not valid JSON must be rejected alone, with its
index, while the other lines in the batch are still accepted."""

import json
import sys
import threading
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cardinal.ingest.server import handler_for
from cardinal.logs.sink import Sink
from cardinal.store.db import connect
from cardinal_harness.product import run_bounded

TOKEN = "ingest-token"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path):
    return tmp_path / "cardinal-home"


@pytest.fixture
def server(home):
    db = connect(home / "store.db")
    sink = Sink(home / "logs", db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(TOKEN, {"example/app"}, sink))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        db.close()


def post(port: int, body: bytes) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/records", data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/x-ndjson"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def record() -> dict:
    return {"schema_version": 1, "id": uuid.uuid4().hex, "at": datetime.now(UTC).isoformat(),
            "level": "error", "event": "request_failed", "message": "request_failed error",
            "source": {"repo": "example/app", "component": "api"}}


def logs_query(home: Path) -> list[dict]:
    result = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), "logs", "query"],
                         timeout=30, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def test_ingest_malformed_ndjson_line_rejected_alone(server, home):
    good = [record(), record()]
    lines = [json.dumps(good[0]), '{"level": "error"', json.dumps(good[1])]
    status, reply = post(server, ("\n".join(lines) + "\n").encode())
    assert status == 200, f"one malformed NDJSON line failed the whole batch: {status} {reply}"
    assert reply["accepted"] == 2, reply
    assert len(reply["rejected"]) == 1, reply
    assert reply["rejected"][0]["index"] == 1, reply
    assert "not valid JSON" in reply["rejected"][0]["error"], reply

    rows = logs_query(home)
    assert sorted(row["record"]["id"] for row in rows) == sorted(item["id"] for item in good)


def test_ingest_malformed_and_schema_invalid_lines_indexed_in_order(server, home):
    invalid = {"schema_version": 1, "level": "loud"}
    lines = [json.dumps(record()), "", json.dumps(invalid), "{oops", json.dumps(record())]
    status, reply = post(server, "\n".join(lines).encode())
    assert status == 200, reply
    assert reply["accepted"] == 2, reply
    # indexes count non-blank lines only
    assert [item["index"] for item in reply["rejected"]] == [1, 2], reply
    assert len(logs_query(home)) == 2


def test_ingest_invalid_utf8_still_400(server):
    status, reply = post(server, b"\xff\xfe\xfa not utf-8\n")
    assert status == 400, reply
    assert "error" in reply


def test_ingest_ndjson_with_no_valid_line_still_400(server, home):
    status, reply = post(server, b"{\"level\": \n{oops\n")
    assert status == 400, reply
    assert reply["error"].startswith("body is not JSON or NDJSON")
    assert logs_query(home) == []
