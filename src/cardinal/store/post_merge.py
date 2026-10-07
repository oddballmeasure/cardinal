"""Post-merge CI judgements. These writes raise: losing one would re-run CI or file an issue twice."""

import json
import sqlite3

from cardinal.store.db import now


def pending(db: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    """Merged runs whose merge commit's CI is not judged yet, oldest first."""
    return db.execute(
        "SELECT runs.run_id, runs.issue, runs.pr_number, runs.merge_sha, runs.ended_at, post_merge.rerun_checks"
        " FROM runs LEFT JOIN post_merge ON post_merge.repo = runs.repo AND post_merge.merge_sha = runs.merge_sha"
        " WHERE runs.repo = ? AND runs.status = 'done' AND runs.merge_sha IS NOT NULL AND runs.ended_at IS NOT NULL"
        " AND (post_merge.verdict IS NULL OR post_merge.verdict = 'rerun') ORDER BY runs.ended_at",
        (repo,),
    ).fetchall()


def record(db: sqlite3.Connection, repo: str, merge_sha: str, run_id: str, verdict: str,
           rerun_checks: list[int], follow_up: int | None = None) -> None:
    db.execute(
        "INSERT INTO post_merge (repo, merge_sha, run_id, verdict, rerun_checks, follow_up, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(repo, merge_sha) DO UPDATE SET verdict = excluded.verdict,"
        " rerun_checks = excluded.rerun_checks, follow_up = excluded.follow_up, updated_at = excluded.updated_at",
        (repo, merge_sha, run_id, verdict, json.dumps(rerun_checks), follow_up, now()),
    )


def is_follow_up(db: sqlite3.Connection, repo: str, issue: int) -> bool:
    return db.execute("SELECT 1 FROM post_merge WHERE repo = ? AND follow_up = ?", (repo, issue)).fetchone() is not None
