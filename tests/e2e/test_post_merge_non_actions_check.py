import json
from datetime import UTC, datetime, timedelta

from cardinal.config.models import Repo
from cardinal.daemon import post_merge as judging
from cardinal.github import issues
from cardinal.store.db import connect

SLUG = "owner/name"
SHA = "a" * 40


def check(id_, name, conclusion, details_url):
    return {"id": id_, "name": name, "head_sha": SHA, "status": "completed",
            "conclusion": conclusion, "details_url": details_url}


def test_failed_non_actions_check_does_not_block_follow_up_after_rerun(tmp_path, monkeypatch):
    db = connect(tmp_path / "store.db")
    ended = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, pr_number, merge_sha, started_at, ended_at)"
               " VALUES ('run-1', 'thread-1', ?, 7, 'done', 42, ?, ?, ?)", (SLUG, SHA, ended, ended))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])

    codecov = check(2, "codecov", "failure", "https://codecov.io/gh/owner/name/commit/" + SHA)
    checks = {"build": check(1, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/1"),
              "codecov": codecov}
    reruns, created = [], []
    monkeypatch.setattr(judging, "latest_runs", lambda slug, sha: dict(checks))
    monkeypatch.setattr(judging, "gh", lambda *args: reruns.append(args) or "")
    monkeypatch.setattr(judging, "failure_log", lambda slug, sha, failed: "log")
    monkeypatch.setattr(issues, "create", lambda slug, title, body, labels: created.append(title) or 99)

    def stored():
        return db.execute("SELECT verdict, rerun_checks, follow_up FROM post_merge WHERE merge_sha = ?",
                          (SHA,)).fetchone()

    judging.judge_all(repo, db)
    assert reruns == [("run", "rerun", "123", "--failed", "-R", SLUG)]
    assert stored()["verdict"] == "rerun"
    assert json.loads(stored()["rerun_checks"]) == [1], "only the re-run Actions check should be recorded"

    # build's re-run finishes and fails again; codecov was never re-run, so it keeps its id.
    checks["build"] = check(3, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/3")
    judging.judge_all(repo, db)
    row = stored()
    assert row["verdict"] == "filed", dict(row)
    assert row["follow_up"] == 99
    assert created == ["CI failed on main after #42: build, codecov"]
