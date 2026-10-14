"""One SQLite connection per process, schema applied on open. Parallel runs are separate processes
writing one store; the busy timeout covers their short, overlapping writes.

Opening is serialised across processes by a lock file beside the store: two processes creating a new
store at once (a daemon and a `status` poll) race to switch it to WAL and create its tables, and SQLite
answers one of them "database is locked" at once, without waiting out the busy timeout, when waiting
could deadlock."""

import fcntl
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f"{path.name}.open.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            connection = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(resources.files("cardinal.store").joinpath("schema.sql").read_text())
            migrate(connection)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS leaves old tables as they were."""
    for table, columns in (("claims", (("host", "TEXT"), ("pid", "INTEGER"))),
                           ("agent_calls", (("model", "TEXT"), ("window", "TEXT"), ("utilization", "REAL")))):
        have = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for column, kind in columns:
            if column not in have:
                try:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN \"{column}\" {kind}")
                except sqlite3.OperationalError as exc:  # another process added it first
                    if "duplicate column" not in str(exc):
                        raise


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
