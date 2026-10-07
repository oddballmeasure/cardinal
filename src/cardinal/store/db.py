"""One SQLite connection per process, schema applied on open. Parallel runs are separate processes
writing one store; the busy timeout covers their short, overlapping writes."""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(resources.files("cardinal.store").joinpath("schema.sql").read_text())
    migrate(connection)
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS leaves old tables as they were."""
    have = {row["name"] for row in connection.execute("PRAGMA table_info(claims)")}
    for column, kind in (("host", "TEXT"), ("pid", "INTEGER")):
        if column not in have:
            try:
                connection.execute(f"ALTER TABLE claims ADD COLUMN {column} {kind}")
            except sqlite3.OperationalError as exc:  # another process added it first
                if "duplicate column" not in str(exc):
                    raise


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
