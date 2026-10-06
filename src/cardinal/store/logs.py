"""Read stored log records in sequence order without reconstructing their JSON."""

import sqlite3

from cardinal.logs.record import LEVELS


def query(db: sqlite3.Connection, level: str | None = None, repo: str | None = None,
          fingerprint: str | None = None, after_seq: int | None = None,
          limit: int | None = None) -> sqlite3.Cursor:
    clauses, params = [], []
    if level is not None:
        thresholds = LEVELS[LEVELS.index(level):]
        clauses.append("level IN (" + ", ".join("?" for _ in thresholds) + ")")
        params.extend(thresholds)
    if repo is not None:
        clauses.append("repo = ?")
        params.append(repo)
    if fingerprint is not None:
        clauses.append("fingerprint = ?")
        params.append(fingerprint)
    if after_seq is not None:
        clauses.append("seq > ?")
        params.append(after_seq)
    sql = "SELECT seq, record FROM logs"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY seq ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params)
