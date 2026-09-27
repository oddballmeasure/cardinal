"""Group new error records by fingerprint and decide, by rule, which deserve an issue."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from cardinal.config.models import Monitor

SERIOUS = ("error", "critical")


@dataclass
class Group:
    fingerprint: str
    repo: str
    first_seq: int
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    urgent: bool = False
    sample: dict = field(default_factory=dict)
    run_ids: list[str] = field(default_factory=list)

    def evidence(self) -> dict:
        return {"fingerprint": self.fingerprint, "repo": self.repo, "count": self.count,
                "first_seen": self.first_seen, "last_seen": self.last_seen, "run_ids": self.run_ids,
                "sample": self.sample}


def new_groups(db: sqlite3.Connection, after: int) -> tuple[list[Group], int]:
    """Fingerprints with serious records after the cursor, and the highest seq read."""
    rows = db.execute("SELECT seq, fingerprint, repo FROM logs WHERE seq > ? AND level IN (?, ?) ORDER BY seq",
                      (after, *SERIOUS)).fetchall()
    top = db.execute("SELECT COALESCE(MAX(seq), ?) FROM logs", (after,)).fetchone()[0]
    groups: dict[str, Group] = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], Group(row["fingerprint"], row["repo"], row["seq"]))
    return list(groups.values()), top


def fill(db: sqlite3.Connection, group: Group, settings: Monitor) -> Group:
    """Count the fingerprint across the whole window, not just since the cursor."""
    since = (datetime.now(UTC) - timedelta(hours=settings.window_hours)).isoformat()
    rows = db.execute("SELECT at, level, failure_kind, run_id, record FROM logs WHERE fingerprint = ? AND at >= ?"
                      " AND level IN (?, ?) ORDER BY seq", (group.fingerprint, since, *SERIOUS)).fetchall()
    group.count = len(rows)
    if rows:
        group.first_seen, group.last_seen = rows[0]["at"], rows[-1]["at"]
        group.sample = json.loads(rows[-1]["record"])
        group.urgent = any(row["level"] == "critical" or row["failure_kind"] == "crash" for row in rows)
        group.run_ids = sorted({row["run_id"] for row in rows if row["run_id"]})[:20]
    return group


def due(group: Group, settings: Monitor) -> bool:
    return group.urgent or group.count >= settings.min_occurrences
