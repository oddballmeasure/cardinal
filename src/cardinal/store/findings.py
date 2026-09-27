"""The monitor's cursor and filed findings. These writes raise: losing one would re-file an issue."""

import sqlite3

from cardinal.store.db import now


def cursor(db: sqlite3.Connection) -> int:
    row = db.execute("SELECT seq FROM monitor_cursor WHERE name = 'monitor'").fetchone()
    return row["seq"] if row else 0


def advance(db: sqlite3.Connection, seq: int) -> None:
    db.execute("INSERT INTO monitor_cursor (name, seq) VALUES ('monitor', ?)"
               " ON CONFLICT(name) DO UPDATE SET seq = excluded.seq", (seq,))


def get(db: sqlite3.Connection, fingerprint: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM findings WHERE fingerprint = ?", (fingerprint,)).fetchone()


def record(db: sqlite3.Connection, fingerprint: str, repo: str, issue: int, occurrences: int,
           first_seen: str, last_seen: str) -> None:
    db.execute(
        "INSERT INTO findings (fingerprint, repo, issue, occurrences, first_seen, last_seen, filed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET repo = excluded.repo,"
        " issue = excluded.issue, occurrences = excluded.occurrences, last_seen = excluded.last_seen,"
        " filed_at = excluded.filed_at",
        (fingerprint, repo, issue, occurrences, first_seen, last_seen, now()),
    )


def seen(db: sqlite3.Connection, fingerprint: str, occurrences: int, last_seen: str) -> None:
    db.execute("UPDATE findings SET occurrences = ?, last_seen = ? WHERE fingerprint = ?",
               (occurrences, last_seen, fingerprint))
