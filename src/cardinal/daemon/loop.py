"""Poll one repository for ready issues and run them one at a time, in issue-number order."""

import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from cardinal import app
from cardinal.config.models import Config, Repo
from cardinal.daemon import post_merge
from cardinal.github import issues
from cardinal.graph.failures import FailureKind
from cardinal.home import Home
from cardinal.store import runs

log = logging.getLogger(__name__)


def retry_sweep(repo: Repo, db: sqlite3.Connection) -> list[int]:
    """Requeue errored issues whose last failure is retryable and older than the retry window.
    The requeued issue goes through ordinary intake: a retry is not a second dispatch path."""
    if repo.retry_after_hours is None:
        return []
    requeued = []
    cutoff = datetime.now(UTC) - timedelta(hours=repo.retry_after_hours)
    for number in issues.with_label(repo.slug, repo.labels.error):
        row = runs.latest(db, repo.slug, number)
        if row is None or row["status"] != "failed" or not row["ended_at"]:
            continue
        if not FailureKind(row["failure_kind"] or "crash").retryable:
            continue
        if datetime.fromisoformat(row["ended_at"]) <= cutoff:
            issues.set_state(repo.slug, number, repo.labels, repo.labels.ready)
            requeued.append(number)
    return requeued


def drain(home: Home, config: Config, repo: Repo, db: sqlite3.Connection) -> list[dict]:
    """Run every ready issue, re-polling after each so work labelled meanwhile (including a follow-up
    filed for a failed merge) is included. CI on earlier merges is judged before each claim: a queue of
    hour-long runs would otherwise leave a merge unjudged until it expired."""
    issues.ensure_labels(repo.slug, repo.labels)
    requeued = retry_sweep(repo, db)
    if requeued:
        log.info("requeued %s after their retry window", requeued, extra={"event": "retry_requeued",
                                                                           "data": {"issues": requeued}})
    results: list[dict] = []
    attempted: set[int] = set()
    while True:
        post_merge.judge_all(repo, db)
        pending = [number for number in issues.ready(repo.slug, repo.labels.ready) if number not in attempted]
        if not pending:
            return results
        number = pending[0]
        attempted.add(number)
        log.info("claiming issue #%s", number)
        try:
            results.append(app.start(home, config, repo, db, number))
        except ValueError as exc:  # already claimed or closed: not ours to run this time
            log.warning("skipped issue #%s: %s", number, exc, extra={"event": "issue_skipped", "data": {"issue": number}})
            results.append({"issue": number, "status": "skipped", "reason": str(exc)})


def serve(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, interval: int) -> None:
    while True:
        try:
            for result in drain(home, config, repo, db):
                log.info("issue #%s settled %s", result["issue"], result["status"])
        except Exception:  # noqa: BLE001 - one failed poll (GitHub down, store locked) must not end the daemon
            log.exception("daemon poll failed; retrying in %ss", interval, extra={"event": "daemon_poll_failed"})
        time.sleep(interval)
