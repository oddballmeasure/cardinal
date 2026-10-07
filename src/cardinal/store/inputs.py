"""State Cardinal acts on. These writes raise: silently losing one would re-claim or re-pay."""

import json
import os
import socket
import sqlite3

from cardinal.contracts.profile import RepoProfile
from cardinal.store.db import now


def load_profile(db: sqlite3.Connection, repo: str) -> RepoProfile | None:
    row = db.execute("SELECT profile FROM repo_profiles WHERE repo = ?", (repo,)).fetchone()
    return RepoProfile.model_validate_json(row["profile"]) if row else None


def save_profile(db: sqlite3.Connection, profile: RepoProfile) -> None:
    db.execute(
        "INSERT INTO repo_profiles (repo, revision, content_hash, profile, updated_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(repo) DO UPDATE SET revision = excluded.revision, content_hash = excluded.content_hash,"
        " profile = excluded.profile, updated_at = excluded.updated_at",
        (profile.repository, profile.revision, profile.content_hash,
         json.dumps(profile.model_dump(), ensure_ascii=False), now()),
    )


def claim(db: sqlite3.Connection, repo: str, issue: int, run_id: str) -> bool:
    """True only for the caller that inserted the row; a held claim is never taken over."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO claims (repo, issue, run_id, claimed_at, host, pid) VALUES (?, ?, ?, ?, ?, ?)",
        (repo, issue, run_id, now(), socket.gethostname(), os.getpid()),
    )
    return cursor.rowcount == 1


def orphaned_claims(db: sqlite3.Connection, repo: str) -> list[sqlite3.Row]:
    """Claims held by a process on this host that no longer exists (killed, out of memory): nothing
    will ever release them. Claims from other hosts, or from before owners were recorded, are left."""
    rows = db.execute("SELECT * FROM claims WHERE repo = ? AND host = ? AND pid IS NOT NULL",
                      (repo, socket.gethostname())).fetchall()
    return [row for row in rows if not alive(row["pid"])]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def release(db: sqlite3.Connection, repo: str, issue: int) -> None:
    db.execute("DELETE FROM claims WHERE repo = ? AND issue = ?", (repo, issue))
