"""A record ingested while `cardinal monitor --once` scans must not be skipped by the cursor.

Each pass is the real CLI in its own process over a real home. For the racing pass, a small launcher
starts the same CLI entry point with the store wrapped so that, right after the scan's rows query,
a second connection to the store file (standing in for `cardinal ingest`, a separate process writing
the same store) inserts a critical record."""

import json
import os
import sqlite3
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]

# min_occurrences is high so the seeded errors are never due; a critical record is due on one occurrence.
MONITOR = "\n[monitor]\nmin_occurrences = 10\nwindow_hours = 24\nmax_issues_per_pass = 3\n"

LAUNCHER = '''\
import json
import sqlite3
import sys
import uuid
from datetime import UTC, datetime

from cardinal.store import db as store

original = store.connect


class IngestDuringScan:
    """Passes every statement to the store; after the scan's rows query, another writer inserts."""

    def __init__(self, connection, path):
        self.connection, self.path, self.fired = connection, path, False

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        if not self.fired and "seq > ?" in sql and "level IN" in sql:
            rows = cursor.fetchall()
            self.fired = True
            writer = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            record = {"level": "critical", "fingerprint": "fp-late"}
            writer.execute("INSERT INTO logs (id, at, level, repo, component, event, failure_kind, fingerprint,"
                           " run_id, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (uuid.uuid4().hex, datetime.now(UTC).isoformat(), "critical", "elsewhere/app", "app",
                            "boom", None, "fp-late", None, json.dumps(record)))
            writer.close()
            return Rows(rows)
        return cursor

    def __getattr__(self, name):
        return getattr(self.connection, name)


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


def connect(path):
    return IngestDuringScan(original(path), path)


store.connect = connect  # before the CLI imports it
from cardinal.cli.main import main  # noqa: E402

raise SystemExit(main(sys.argv[1:]))
'''


def environment() -> dict:
    path = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *([path] if path else [])])}


def cardinal(home: Path, *args: str, launcher: Path | None = None):
    entry = [str(launcher)] if launcher else ["-m", "cardinal.cli.main"]
    return run_bounded([sys.executable, *entry, "--home", str(home), *args], timeout=60, cwd=ROOT,
                       env=environment())


def monitored_home(tmp_path: Path) -> Path:
    home = tmp_path / "cardinal-home"
    init = cardinal(home, "init", "--repo", "team/app")
    assert init.returncode == 0, init.stderr
    config = home / "cardinal.toml"
    # The monitor's own records stay out of the store, so every seq below is one the test wrote.
    config.write_text(config.read_text().replace('level = "info"', 'level = "critical"') + MONITOR)
    return home


def insert(home: Path, level: str, fingerprint: str, repo: str = "team/app") -> None:
    record = {"level": level, "fingerprint": fingerprint}
    with sqlite3.connect(home / "store.db") as db:
        db.execute("INSERT INTO logs (id, at, level, repo, component, event, failure_kind, fingerprint, run_id,"
                   " record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (uuid.uuid4().hex, datetime.now(UTC).isoformat(), level, repo, "app", "boom", None,
                    fingerprint, None, json.dumps(record)))


def cursor(home: Path) -> int:
    with sqlite3.connect(home / "store.db") as db:
        row = db.execute("SELECT seq FROM monitor_cursor WHERE name = 'monitor'").fetchone()
    return row[0] if row else 0


def monitor_once(home: Path, launcher: Path | None = None) -> list[dict]:
    result = cardinal(home, "monitor", "--once", launcher=launcher)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout)


def test_monitor_cursor_does_not_skip_record_ingested_during_scan(tmp_path):
    home = monitored_home(tmp_path)
    insert(home, "error", "fp-a")
    insert(home, "error", "fp-b")
    launcher = tmp_path / "launcher" / "ingest_during_scan.py"
    launcher.parent.mkdir()
    launcher.write_text(LAUNCHER)

    assert monitor_once(home, launcher) == []  # fp-a and fp-b are read but below min_occurrences
    with sqlite3.connect(home / "store.db") as db:
        late = db.execute("SELECT seq FROM logs WHERE fingerprint = 'fp-late'").fetchone()
    assert late == (3,), "the concurrent writer's critical record was not stored during the pass"
    assert cursor(home) == 2, f"cursor covers seq {cursor(home)}, but only seq <= 2 was read"

    # The next pass reads the late critical record: it is due on one occurrence, and filing it fails
    # only because its repository is not configured, so the cursor is held just below it.
    later = monitor_once(home)
    assert [(item["fingerprint"], item["action"]) for item in later] == [("fp-late", "failed")], later
    assert "elsewhere/app" in later[0]["error"]
    assert cursor(home) == 2


def test_monitor_cursor_still_reaches_highest_seq_without_concurrent_insert(tmp_path):
    home = monitored_home(tmp_path)
    for level in ("error", "info", "error", "info", "error"):
        insert(home, level, f"fp-{level}")

    assert monitor_once(home) == []
    assert cursor(home) == 5
    assert monitor_once(home) == [] and cursor(home) == 5
