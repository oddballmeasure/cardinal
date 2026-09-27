"""Where records go: a daily JSONL file (the portable archive) and the store's `logs` table (what the
monitor queries). A failed write is reported on stderr and dropped; logging must never stop work.
It prints rather than logs, because a log call here would re-enter this sink."""

import fcntl
import json
import sqlite3
import sys
import threading
from pathlib import Path

from cardinal.logs.fingerprint import stamped
from cardinal.logs.record import LogRecord


class Sink:
    def __init__(self, directory: Path, db: sqlite3.Connection) -> None:
        self.directory = directory
        self.db = db
        self.lock = threading.Lock()
        self.reported: set[str] = set()

    def write(self, record: LogRecord) -> LogRecord:
        record = stamped(record)
        line = record.model_dump_json()
        with self.lock:
            self._attempt("jsonl", lambda: self._append(record, line))
            self._attempt("store", lambda: self._insert(record, line))
        return record

    def _append(self, record: LogRecord, line: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.directory / f"{record.at:%Y-%m-%d}.jsonl", "a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)  # the daemon, ingest and monitor may share a day's file
            handle.write(line + "\n")

    def _insert(self, record: LogRecord, line: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO logs (id, at, level, repo, component, event, failure_kind, fingerprint, run_id, record)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.id, record.at.isoformat(), record.level, record.source.repo, record.source.component,
             record.event, record.failure_kind, record.fingerprint, record.context.run_id, line),
        )

    def _attempt(self, target: str, action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - see module docstring
            key = f"{target}:{type(exc).__name__}"
            if key not in self.reported:  # once per cause, or a full disk floods stderr
                self.reported.add(key)
                print(f"cardinal: log {target} write failed and records are being dropped: {exc!r}", file=sys.stderr)
