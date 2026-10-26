"""POST /v1/records must not count a record as accepted when the store's `logs` table did not
receive it: the sender needs a rejection so it can resend. Cardinal's own logging into the same
failing store must still never raise."""

import json
import logging
import threading
import urllib.request
import uuid
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

from cardinal.ingest.server import handler_for
from cardinal.logs.handler import CardinalHandler
from cardinal.logs.sink import Sink
from cardinal.store.db import connect

TOKEN = "ingest-token"
FAIL_LOGS = "CREATE TRIGGER fail_logs BEFORE INSERT ON logs BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END"


def record() -> dict:
    return {"schema_version": 1, "id": uuid.uuid4().hex, "at": datetime.now(UTC).isoformat(),
            "level": "error", "event": "request_failed", "message": "request_failed error",
            "source": {"repo": "example/app", "component": "api"}}


def start(tmp_path, failing: bool):
    db = connect(tmp_path / "store.db")
    if failing:
        db.execute(FAIL_LOGS)
    sink = Sink(tmp_path / "logs", db)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(TOKEN, {"example/app"}, sink))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, db


def post(port: int, items: list) -> tuple[int, dict]:
    body = "".join(json.dumps(item) + "\n" for item in items).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/records", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {TOKEN}",
                                              "Content-Type": "application/x-ndjson"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


@pytest.mark.parametrize("failing", [True, False], ids=["store_fails", "store_works"])
def test_ingest_does_not_accept_records_the_store_failed_to_save(tmp_path, failing):
    httpd, db = start(tmp_path, failing)
    try:
        status, reply = post(httpd.server_port, [record()])
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert status == 200, reply
    rows = db.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
    if failing:
        assert rows == 0
        assert reply["accepted"] == 0, f"record the store dropped was reported accepted: {reply}"
        assert len(reply["rejected"]) == 1, reply
        assert reply["rejected"][0]["index"] == 0
        assert "could not be stored" in str(reply["rejected"][0]["error"])
    else:
        assert reply == {"accepted": 1, "rejected": []}
        assert rows == 1


def test_ingest_duplicate_id_is_still_accepted(tmp_path):
    httpd, db = start(tmp_path, failing=False)
    item = record()
    try:
        first = post(httpd.server_port, [item])
        second = post(httpd.server_port, [item])
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert first == (200, {"accepted": 1, "rejected": []})
    assert second == (200, {"accepted": 1, "rejected": []}), "an id already stored is not a store failure"
    assert db.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 1


def test_cardinal_logging_does_not_raise_when_store_insert_fails(tmp_path, capsys):
    db = connect(tmp_path / "store.db")
    db.execute(FAIL_LOGS)
    sink = Sink(tmp_path / "logs", db)
    handler = CardinalHandler(sink, "example/app", tmp_path, logging.DEBUG)
    # a broken handler would print through handleError; make it raise instead so the test sees it
    handler.handleError = lambda entry: (_ for _ in ()).throw(AssertionError("handler failed"))
    log = logging.getLogger(f"cardinal.test.store_failure.{uuid.uuid4().hex}")
    log.propagate = False
    log.addHandler(handler)
    try:
        log.error("store is failing", extra={"event": "test_event"})
        log.error("store is still failing", extra={"event": "test_event"})
    finally:
        log.removeHandler(handler)
    assert db.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == 0
    err = capsys.readouterr().err
    assert err.count("log store write failed and records are being dropped") == 1, err
    lines = [line for path in (tmp_path / "logs").glob("*.jsonl") for line in path.read_text().splitlines()]
    assert len(lines) == 2  # the JSONL archive still receives Cardinal's own records
