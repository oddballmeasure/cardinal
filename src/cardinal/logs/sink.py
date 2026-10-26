"""Where records go: a daily JSONL file (the portable archive) and the store's `logs` table (what the
monitor queries). A failed write is reported on stderr and dropped; logging must never stop work.
`write_stored` also never raises but tells its caller (ingest) whether the store received the record.
It prints rather than logs, because a log call here would re-enter this sink."""

import fcntl
import json
import sqlite3
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from cardinal.logs.fingerprint import stamped
from cardinal.logs.record import LogRecord


def utc(at: datetime) -> datetime:
    """`at` as a UTC instant, so the TEXT `logs.at` column orders by time; a naive `at` is taken as UTC."""
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


class Sink:
    def __init__(self, directory: Path, db: sqlite3.Connection) -> None:
        self.directory = directory
        self.db = db
        self.lock = threading.Lock()
        self.reported: set[str] = set()

    def write(self, record: LogRecord) -> LogRecord:
        """Cardinal's own logging: never raises, whatever happened to either write."""
        return self._write(record)[0]

    def write_stored(self, record: LogRecord) -> bool:
        """Like `write`, and never raises, but answers whether the store's `logs` table received the
        record (an id already there counts as received). Ingest uses it so a record the store dropped
        is not reported delivered."""
        return self._write(record)[1]

    def _write(self, record: LogRecord) -> tuple[LogRecord, bool]:
        record = stamped(record)
        line = record.model_dump_json()
        with self.lock:
            self._attempt("jsonl", lambda: self._append(record, line))
            stored = self._attempt("store", lambda: self._insert(record, line))
        return record, stored

    def _append(self, record: LogRecord, line: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        # named by the UTC day, the same day the `logs.at` column shows, whatever offset the sender used
        with open(self.directory / f"{utc(record.at):%Y-%m-%d}.jsonl", "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)  # the daemon, ingest and monitor may share a day's file
            handle.write(line + "\n")

    def _insert(self, record: LogRecord, line: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO logs (id, at, level, repo, component, event, failure_kind, fingerprint, run_id, record)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.id, utc(record.at).isoformat(), record.level, record.source.repo, record.source.component,
             record.event, record.failure_kind, record.fingerprint, record.context.run_id, line),
        )

    def _attempt(self, target: str, action) -> bool:
        """True when `action` completed; a failure is reported on stderr and answers False."""
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - see module docstring
            key = f"{target}:{type(exc).__name__}"
            if key not in self.reported:  # once per cause, or a full disk floods stderr
                self.reported.add(key)
                print(f"cardinal: log {target} write failed and records are being dropped: {exc!r}", file=sys.stderr)
            return False
        return True
