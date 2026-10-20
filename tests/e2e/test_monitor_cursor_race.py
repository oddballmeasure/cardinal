"""A record ingested while the monitor's scan runs must not be skipped by the cursor.

The store is a real Cardinal store. A second connection stands in for `cardinal ingest` (a separate
process writing the same store) and inserts a critical record right after new_groups' rows query."""

import json
import uuid

from cardinal.monitor.scan import new_groups
from cardinal.store.db import connect, now


def _insert(db, level, fingerprint):
    record = {"level": level, "fingerprint": fingerprint}
    db.execute("INSERT INTO logs (id, at, level, repo, component, event, failure_kind, fingerprint, run_id, record)"
               " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (uuid.uuid4().hex, now(), level, "team/app", "app", "boom", None, fingerprint, None,
                json.dumps(record)))


class _IngestDuringScan:
    """Passes every statement to the store; after the first rows query, another writer inserts."""

    def __init__(self, db, writer):
        self.db, self.writer, self.fired = db, writer, False

    def execute(self, sql, params=()):
        cursor = self.db.execute(sql, params)
        if not self.fired and "level IN" in sql:
            rows = cursor.fetchall()
            self.fired = True
            _insert(self.writer, "critical", "fp-late")
            return _Rows(rows)
        return cursor

    def __getattr__(self, name):
        return getattr(self.db, name)


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def test_monitor_cursor_does_not_skip_record_ingested_during_scan(tmp_path):
    path = tmp_path / "home" / "store.db"
    db = connect(path)
    writer = connect(path)
    _insert(db, "error", "fp-a")
    _insert(db, "error", "fp-b")

    groups, top = new_groups(_IngestDuringScan(db, writer), 0)
    assert sorted(g.fingerprint for g in groups) == ["fp-a", "fp-b"]
    assert top == 2, f"cursor covers seq {top}, but only seq <= 2 was read"

    later, _ = new_groups(db, top)
    assert [g.fingerprint for g in later] == ["fp-late"], later


def test_monitor_cursor_still_reaches_highest_seq_without_concurrent_insert(tmp_path):
    db = connect(tmp_path / "home" / "store.db")
    for level in ("error", "info", "error", "info", "error"):
        _insert(db, level, f"fp-{level}")
    _, top = new_groups(db, 0)
    assert top == 5
