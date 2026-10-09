"""What became of each filed proposal, read back from its issue at the start of every pass.

A person approves by swapping `proposed` for `ready` (or the daemon has already run it) and rejects
by closing it, auto-filed issues included; the closing comment is kept as the reason, and the next survey reads it. Landing is
the daemon's done or error label. Product decisions are kept out of the track record."""

import logging
import sqlite3

from cardinal.config.models import Repo
from cardinal.github import issues
from cardinal.store import runs
from cardinal.store import scout as store

log = logging.getLogger(__name__)
WINDOW_DAYS = 60  # how far back rejections are remembered and track records are counted


def read(repo: Repo, db: sqlite3.Connection, row: sqlite3.Row) -> tuple[str | None, str | None]:
    labels = repo.labels
    _, current, state = issues.view(repo.slug, row["issue"])
    run = runs.latest(db, repo.slug, row["issue"])
    if labels.done in current:
        return "done", None
    if labels.error in current:
        return "error", run["failure_kind"] if run else None
    # An auto-filed issue starts ready: that is the scout's own decision, not a person's approval.
    if row["filed_mode"] != "auto" and (labels.ready in current or labels.working in current or run is not None):
        return "approved", None
    if state == "CLOSED":
        return "rejected", issues.last_comment(repo.slug, row["issue"])[:1000] or "Closed without a comment"
    return row["outcome"], row["outcome_reason"]


def track(repo: Repo, db: sqlite3.Connection) -> list[dict]:
    changes = []
    for row in store.undecided(db, repo.slug):
        try:
            outcome, reason = read(repo, db, row)
        except Exception as exc:  # noqa: BLE001 - one unreadable issue must not stop the pass; it is read again next time
            log.error("could not read the outcome of #%s", row["issue"], exc_info=exc, extra={"event": "scout_outcome_failed"})
            continue
        if outcome != row["outcome"]:
            store.set_outcome(db, row["id"], outcome, reason)
            changes.append({"issue": row["issue"], "title": row["title"], "outcome": outcome, "reason": reason})
    return changes


def track_record(db: sqlite3.Connection, slug: str) -> dict[str, dict]:
    """Per category over the window: approval is approved / decided, done-rate is done / landed."""
    record: dict[str, dict] = {}
    for row in store.decided(db, slug, WINDOW_DAYS):
        counts = record.setdefault(row["category"], {"decided": 0, "approved": 0, "rejected": 0, "done": 0, "error": 0})
        counts["decided"] += 1
        counts["rejected" if row["outcome"] == "rejected" else "approved"] += 1
        if row["outcome"] in ("done", "error"):
            counts[row["outcome"]] += 1
    for counts in record.values():
        landed = counts["done"] + counts["error"]
        counts["approval"] = round(counts["approved"] / counts["decided"], 3)
        counts["done_rate"] = round(counts["done"] / landed, 3) if landed else None
    return record
