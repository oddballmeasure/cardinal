"""Judge every check on each merge commit Cardinal made, once they finish.

Cardinal merges when the required checks pass on the PR head; slower checks and the base branch's
own CI on the merge commit finish later. A failure is re-run once to filter flakes; a second failure
files one follow-up issue, labelled ready so the daemon fixes it like any other. A follow-up whose
own merge fails again goes to a person instead, so Cardinal never chases its own tail.
"""

import json
import logging
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from cardinal.config.models import Repo
from cardinal.github import issues
from cardinal.github.gh import gh
from cardinal.github.prs import PASSED, failure_log, latest_runs
from cardinal.store import post_merge

log = logging.getLogger(__name__)

GIVE_UP = timedelta(hours=6)
LISTING_GRACE = timedelta(minutes=10)  # a fresh merge's checks may not be listed yet
ACTIONS_RUN = re.compile(r"/actions/runs/(\d+)/")


def judge_all(repo: Repo, db: sqlite3.Connection) -> None:
    """Never raises: one merge that cannot be judged now is tried again on the next poll."""
    try:
        rows = post_merge.pending(db, repo.slug)
    except Exception:  # noqa: BLE001 - the poll goes on to its ready issues
        log.exception("post-merge judging could not read the store", extra={"event": "post_merge_failed"})
        return
    for row in rows:
        try:
            judge(repo, db, row)
        except Exception:  # noqa: BLE001 - logged with its traceback; retried next poll
            log.exception("post-merge judging of %s failed", row["merge_sha"][:12],
                          extra={"event": "post_merge_failed", "data": {"merge_sha": row["merge_sha"]}})


def judge(repo: Repo, db: sqlite3.Connection, row: sqlite3.Row) -> None:
    sha, run_id = row["merge_sha"], row["run_id"]
    rerun = json.loads(row["rerun_checks"] or "[]")
    age = datetime.now(UTC) - datetime.fromisoformat(row["ended_at"])
    if age > GIVE_UP:
        post_merge.record(db, repo.slug, sha, run_id, "expired", rerun)
        log.warning("gave up judging CI on merge %s after %s", sha[:12], GIVE_UP,
                    extra={"event": "post_merge_expired", "data": {"merge_sha": sha, "issue": row["issue"]}})
        return
    checks = latest_runs(repo.slug, sha)
    if not checks:
        if age > LISTING_GRACE:
            post_merge.record(db, repo.slug, sha, run_id, "no_checks", rerun)
        return
    if any(check["status"] != "completed" or check["id"] in rerun for check in checks.values()):
        return  # still running, or a re-run has not replaced its failed check yet
    failed = sorted(name for name, check in checks.items() if check["conclusion"] not in PASSED)
    if not failed:
        post_merge.record(db, repo.slug, sha, run_id, "passed", rerun)
        log.info("CI passed on merge %s", sha[:12], extra={"event": "post_merge_passed", "data": {"merge_sha": sha}})
        return
    runs = sorted({match.group(1) for name in failed
                   if (match := ACTIONS_RUN.search(checks[name].get("details_url") or ""))})
    if not rerun and runs:
        for number in runs:
            gh("run", "rerun", number, "--failed", "-R", repo.slug)
        post_merge.record(db, repo.slug, sha, run_id, "rerun", [checks[name]["id"] for name in failed])
        log.warning("re-ran %s failed on merge %s", failed, sha[:12],
                    extra={"event": "post_merge_rerun", "data": {"merge_sha": sha, "checks": failed, "runs": runs}})
        return
    file_follow_up(repo, db, row, failed, rerun)


def file_follow_up(repo: Repo, db: sqlite3.Connection, row: sqlite3.Row, failed: list[str], rerun: list[int]) -> None:
    sha, pr, issue = row["merge_sha"], row["pr_number"], row["issue"]
    looping = post_merge.is_follow_up(db, repo.slug, issue)
    label = repo.labels.needs_human if looping else repo.labels.ready
    title = f"CI failed on {repo.base_branch} after #{pr}: {', '.join(failed)}"
    body = (f"Cardinal merged #{pr} (for #{issue}) as `{sha}`. Afterwards these checks failed on `{repo.base_branch}` "
            f"at that commit{', and again when re-run' if rerun else ''}: {', '.join(f'`{name}`' for name in failed)}.\n\n"
            + (f"#{issue} was itself filed for a post-merge CI failure, so this needs a person.\n\n" if looping else "")
            + "```\n" + failure_log(repo.slug, sha, failed).replace("```", "'''") + "\n```\n")
    post_merge.record(db, repo.slug, sha, row["run_id"], "filing", rerun)  # a crash after this never files twice
    number = issues.create(repo.slug, title, body, (label,))
    post_merge.record(db, repo.slug, sha, row["run_id"], "filed", rerun, number)
    log.warning("filed #%s: %s", number, title, extra={"event": "post_merge_filed",
                                                      "data": {"merge_sha": sha, "issue": number, "label": label}})
