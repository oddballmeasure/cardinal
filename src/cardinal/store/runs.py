"""Run rows. Status is read back by `status`, `resume` and the daemon, so writes here raise."""

import sqlite3

from cardinal.store.db import now

FIELDS = {"status", "failure_kind", "failure_detail", "branch", "head_sha", "pr_number", "pr_url",
          "merge_sha", "deployed_sha", "ended_at"}


def create(db: sqlite3.Connection, run_id: str, thread_id: str, repo: str, issue: int) -> None:
    db.execute(
        "INSERT INTO runs (run_id, thread_id, repo, issue, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
        (run_id, thread_id, repo, issue, now()),
    )


def update(db: sqlite3.Connection, run_id: str, **fields: object) -> None:
    unknown = set(fields) - FIELDS
    if unknown:
        raise ValueError(f"Unknown run fields {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    db.execute(f"UPDATE runs SET {assignments} WHERE run_id = ?", (*fields.values(), run_id))  # noqa: S608 - names checked above


def latest(db: sqlite3.Connection, repo: str, issue: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM runs WHERE repo = ? AND issue = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (repo, issue),
    ).fetchone()


def get(db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row
