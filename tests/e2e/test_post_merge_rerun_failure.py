import json
from datetime import UTC, datetime, timedelta

from cardinal.config.models import Repo
from cardinal.daemon import post_merge as judging
from cardinal.github import issues
from cardinal.github.gh import GhError
from cardinal.store.db import connect

SLUG = "owner/name"
SHA = "a" * 40


def check(id_, name, conclusion, details_url):
    return {"id": id_, "name": name, "head_sha": SHA, "status": "completed",
            "conclusion": conclusion, "details_url": details_url}


def setup(tmp_path):
    db = connect(tmp_path / "store.db")
    ended = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, pr_number, merge_sha, started_at, ended_at)"
               " VALUES ('run-1', 'thread-1', ?, 7, 'done', 42, ?, ?, ?)", (SLUG, SHA, ended, ended))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    return db, repo


def test_failed_rerun_request_files_follow_up_instead_of_retrying_until_expiry(tmp_path, monkeypatch):
    db, repo = setup(tmp_path)
    checks = {"build": check(1, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/1")}
    created = []

    def fake_gh(*args, **kwargs):
        if args[:2] == ("run", "rerun"):
            raise GhError("gh run rerun failed: HTTP 403: Resource not accessible by integration")
        return ""

    monkeypatch.setattr(judging, "latest_runs", lambda slug, sha: dict(checks))
    monkeypatch.setattr(judging, "gh", fake_gh)
    monkeypatch.setattr(judging, "failure_log", lambda slug, sha, failed: "log")
    monkeypatch.setattr(issues, "create",
                        lambda slug, title, body, labels: created.append((title, body, labels)) or 99)

    judging.judge_all(repo, db)

    row = db.execute("SELECT verdict, follow_up FROM post_merge WHERE merge_sha = ?", (SHA,)).fetchone()
    assert row is not None, "merge left unjudged after the rerun request failed; it would be retried until expiry"
    assert row["verdict"] == "filed", dict(row)
    assert row["follow_up"] == 99
    assert len(created) == 1, created
    title, body, labels = created[0]
    assert labels == (repo.labels.ready,)
    assert "these checks failed on `main` at that commit: `build`." in body, body
    assert "again when re-run" not in body, body


def test_partly_failed_rerun_records_only_rerun_checks_and_never_retries(tmp_path, monkeypatch):
    db, repo = setup(tmp_path)
    checks = {"build": check(1, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/1"),
              "lint": check(2, "lint", "failure", f"https://github.com/{SLUG}/actions/runs/456/job/2")}
    reruns, created = [], []

    def fake_gh(*args, **kwargs):
        if args[:2] == ("run", "rerun"):
            reruns.append(args)
            if args[2] == "456":
                raise GhError("gh run rerun failed: HTTP 403: Resource not accessible by integration")
        return ""

    monkeypatch.setattr(judging, "latest_runs", lambda slug, sha: dict(checks))
    monkeypatch.setattr(judging, "gh", fake_gh)
    monkeypatch.setattr(judging, "failure_log", lambda slug, sha, failed: "log")
    monkeypatch.setattr(issues, "create",
                        lambda slug, title, body, labels: created.append((title, body, labels)) or 99)

    def stored():
        return db.execute("SELECT verdict, rerun_checks, follow_up FROM post_merge WHERE merge_sha = ?",
                          (SHA,)).fetchone()

    judging.judge_all(repo, db)
    assert reruns == [("run", "rerun", "123", "--failed", "-R", SLUG),
                      ("run", "rerun", "456", "--failed", "-R", SLUG)]
    assert stored()["verdict"] == "rerun"
    assert json.loads(stored()["rerun_checks"]) == [1]
    assert created == []

    # build's re-run is still going: nothing is re-run or filed.
    checks["build"] = check(3, "build", None, f"https://github.com/{SLUG}/actions/runs/123/job/3")
    checks["build"]["status"] = "in_progress"
    judging.judge_all(repo, db)
    assert len(reruns) == 2 and created == []

    # build's re-run fails again; lint was never re-run and keeps its id.
    checks["build"] = check(3, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/3")
    judging.judge_all(repo, db)
    judging.judge_all(repo, db)
    assert len(reruns) == 2, reruns
    row = stored()
    assert row["verdict"] == "filed", dict(row)
    assert row["follow_up"] == 99
    assert len(created) == 1, created
    title, body, labels = created[0]
    assert title == "CI failed on main after #42: build, lint"
    assert labels == (repo.labels.ready,)
    assert "and again when re-run: `build`. These checks failed and were not re-run: `lint`." in body, body
