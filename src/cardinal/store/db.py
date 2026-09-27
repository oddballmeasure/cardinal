"""One SQLite connection per process, schema applied on open."""

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(resources.files("cardinal.store").joinpath("schema.sql").read_text())
    return connection


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
