"""POST /v1/records must answer a bad Content-Length with 400 and a JSON error, never drop the
connection or read until EOF. It accepts a JSON array, NDJSON, or one JSON object on one line or
several."""

import json
import socket
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cardinal.ingest.server import MAX_BODY, handler_for
from cardinal.logs.sink import Sink

TOKEN = "ingest-token"
EXAMPLE_RECORD = Path(__file__).resolve().parents[2] / "examples" / "log-record.json"
REPOS = {"example/app", "oddballmeasure/cardinal_test_repo"}


@pytest.fixture
def server(tmp_path):
    sink = Sink(tmp_path / "logs", sqlite3.connect(":memory:", check_same_thread=False))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(TOKEN, REPOS, sink))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()


def raw_post(port: int, content_length: str, body: bytes = b"") -> tuple[int | None, bytes]:
    """Send a keep-alive request with the given Content-Length header; the client keeps its side
    open, as a real keep-alive client does while waiting for the reply."""
    request = (f"POST /v1/records HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {TOKEN}\r\n"
               f"Content-Type: application/x-ndjson\r\nContent-Length: {content_length}\r\n"
               "Connection: keep-alive\r\n\r\n").encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(request)
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if b"\r\n\r\n" in data:
                head, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                while len(rest) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                status = int(head.split(b"\r\n")[0].split()[1])
                return status, rest
        except TimeoutError:
            pass
        return None, data


@pytest.mark.parametrize("content_length", ["abc", "-1"])
def test_ingest_rejects_invalid_content_length_with_400_json(server, content_length):
    status, body = raw_post(server, content_length)
    assert status == 400, f"Content-Length {content_length!r}: no 400 response (status={status}, raw={body!r})"
    assert "error" in json.loads(body)


def test_ingest_valid_ndjson_still_accepted(server):
    record = {"schema_version": 1, "id": uuid.uuid4().hex, "at": datetime.now(UTC).isoformat(),
              "level": "error", "event": "request_failed", "message": "request_failed error",
              "source": {"repo": "example/app", "component": "api"}}
    body = (json.dumps(record) + "\n").encode()
    status, reply = raw_post(server, str(len(body)), body)
    assert status == 200, reply
    assert json.loads(reply)["accepted"] == 1


def stored(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for path in sorted((tmp_path / "logs").glob("*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_ingest_accepts_example_record_multiline_as_is(server, tmp_path):
    body = EXAMPLE_RECORD.read_bytes()
    assert body.count(b"\n") > 1, "the example record should be pretty-printed across lines"
    status, reply = raw_post(server, str(len(body)), body)
    assert status == 200, reply
    assert json.loads(reply) == {"accepted": 1, "rejected": []}
    records = stored(tmp_path)
    assert len(records) == 1
    assert records[0]["source"]["repo"] == "oddballmeasure/cardinal_test_repo"
    assert records[0]["message"] == "GET /stats/tags raised ZeroDivisionError"


def test_ingest_two_ndjson_lines_both_accepted(server, tmp_path):
    lines = [json.dumps({"schema_version": 1, "level": "error", "event": "request_failed", "message": f"m{i}",
                         "source": {"repo": "example/app", "component": "api"}}) for i in range(2)]
    body = "\n".join(lines).encode()
    status, reply = raw_post(server, str(len(body)), body)
    assert status == 200, reply
    assert json.loads(reply)["accepted"] == 2
    assert sorted(r["message"] for r in stored(tmp_path)) == ["m0", "m1"]


def test_ingest_json_array_still_accepted(server):
    record = {"schema_version": 1, "level": "error", "event": "request_failed", "message": "m",
              "source": {"repo": "example/app", "component": "api"}}
    body = json.dumps([record, record], indent=2).encode()
    status, reply = raw_post(server, str(len(body)), body)
    assert status == 200, reply
    assert json.loads(reply)["accepted"] == 2


@pytest.mark.parametrize("body", [b"not json", b"[1, 2", b"{\n  \"level\": \n"])
def test_ingest_non_json_body_still_400(server, body):
    status, reply = raw_post(server, str(len(body)), body)
    assert status == 400, reply
    assert json.loads(reply)["error"].startswith("body is not JSON or NDJSON")


def test_ingest_content_length_over_max_body_still_413(server):
    status, reply = raw_post(server, str(MAX_BODY + 1))
    assert status == 413, reply
    assert "error" in json.loads(reply)
