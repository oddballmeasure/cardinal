"""The scout's passes, proposals and areas. These writes raise: a lost row would re-file an issue,
forget an outcome, or survey the same area again."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from cardinal.store.db import now


def ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def start_pass(db: sqlite3.Connection, pass_id: str, repo: str, autonomy: str) -> None:
    db.execute("INSERT INTO scout_passes (pass_id, repo, autonomy, started_at) VALUES (?, ?, ?, ?)",
               (pass_id, repo, autonomy, now()))


def update_pass(db: sqlite3.Connection, pass_id: str, **fields: object) -> None:
    allowed = {"base_sha", "baseline_passed", "counts", "failure_kind", "failure_detail", "ended_at"}
    if not fields or set(fields) - allowed:
        raise ValueError(f"Unknown scout pass fields {sorted(set(fields) - allowed)}")
    assignments = ", ".join(f"{name} = ?" for name in fields)
    db.execute(f"UPDATE scout_passes SET {assignments} WHERE pass_id = ?", (*fields.values(), pass_id))  # noqa: S608 - names checked above


def baseline(db: sqlite3.Connection, repo: str, base_sha: str) -> bool | None:
    row = db.execute("SELECT baseline_passed FROM scout_passes WHERE repo = ? AND base_sha = ?"
                     " AND baseline_passed IS NOT NULL ORDER BY started_at DESC LIMIT 1", (repo, base_sha)).fetchone()
    return None if row is None else bool(row["baseline_passed"])


def last_pass(db: sqlite3.Connection, repo: str) -> dict | None:
    row = db.execute("SELECT * FROM scout_passes WHERE repo = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                     (repo,)).fetchone()
    return dict(row) if row else None


def record(db: sqlite3.Connection, row_id: int | None, *, repo: str, pass_id: str, fingerprint: str,
           proposal: dict, status: str, **fields: object) -> int:
    """Insert a proposal, or update the held row it came from."""
    allowed = ("reason", "verdict", "repro_ok", "filed_mode", "issue")
    if set(fields) - set(allowed):
        raise ValueError(f"Unknown scout proposal fields {sorted(set(fields) - set(allowed))}")
    values = {"repo": repo, "pass_id": pass_id, "fingerprint": fingerprint, "category": proposal["category"],
              "title": proposal["title"], "proposal": json.dumps(proposal, ensure_ascii=False), "status": status,
              **{name: fields.get(name) for name in allowed}}
    if row_id is None:
        names = ", ".join(values)
        marks = ", ".join("?" * len(values))
        cursor = db.execute(f"INSERT INTO scout_proposals ({names}, created_at) VALUES ({marks}, ?)",  # noqa: S608 - fixed names
                            (*values.values(), now()))
        return cursor.lastrowid
    assignments = ", ".join(f"{name} = ?" for name in values)
    db.execute(f"UPDATE scout_proposals SET {assignments} WHERE id = ?", (*values.values(), row_id))  # noqa: S608 - fixed names
    return row_id


def held(db: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM scout_proposals WHERE repo = ? AND status = 'held' ORDER BY id", (repo,)).fetchall()


def latest_filed(db: sqlite3.Connection, repo: str, fingerprint: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM scout_proposals WHERE repo = ? AND fingerprint = ? AND status = 'filed'"
                      " ORDER BY id DESC LIMIT 1", (repo, fingerprint)).fetchone()


def undecided(db: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    """Filed proposals whose outcome can still change."""
    return db.execute("SELECT * FROM scout_proposals WHERE repo = ? AND status = 'filed'"
                      " AND (outcome IS NULL OR outcome IN ('approved', 'error')) ORDER BY id", (repo,)).fetchall()


def set_outcome(db: sqlite3.Connection, row_id: int, outcome: str, reason: str | None) -> None:
    db.execute("UPDATE scout_proposals SET outcome = ?, outcome_reason = ?, decided_at = COALESCE(decided_at, ?)"
               " WHERE id = ?", (outcome, reason, now(), row_id))


def rejections(db: sqlite3.Connection, repo: str, days: float) -> list[dict]:
    rows = db.execute("SELECT issue, category, title, outcome_reason FROM scout_proposals WHERE repo = ?"
                      " AND outcome = 'rejected' AND decided_at >= ? ORDER BY decided_at DESC LIMIT 50",
                      (repo, ago(days))).fetchall()
    return [{"issue": row["issue"], "category": row["category"], "title": row["title"],
             "reason": row["outcome_reason"] or ""} for row in rows]


def decided(db: sqlite3.Connection, repo: str, days: float) -> list[sqlite3.Row]:
    """Filed proposals a person or the daemon has acted on within the window, product decisions aside."""
    return db.execute("SELECT category, outcome FROM scout_proposals WHERE repo = ? AND status = 'filed'"
                      " AND filed_mode != 'needs_human' AND outcome IS NOT NULL AND decided_at >= ?",
                      (repo, ago(days))).fetchall()


def area_times(db: sqlite3.Connection, repo: str) -> dict[str, str]:
    rows = db.execute("SELECT area, last_surveyed FROM scout_areas WHERE repo = ?", (repo,))
    return {row["area"]: row["last_surveyed"] for row in rows}


def surveyed(db: sqlite3.Connection, repo: str, area: str) -> None:
    db.execute("INSERT INTO scout_areas (repo, area, last_surveyed) VALUES (?, ?, ?)"
               " ON CONFLICT(repo, area) DO UPDATE SET last_surveyed = excluded.last_surveyed", (repo, area, now()))
