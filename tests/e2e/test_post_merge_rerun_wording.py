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


def run_scenario(tmp_path, monkeypatch, build_rerun_conclusion, with_codecov=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = connect(tmp_path / "store.db")
    ended = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, pr_number, merge_sha, started_at, ended_at)"
               " VALUES ('run-1', 'thread-1', ?, 7, 'done', 42, ?, ?, ?)", (SLUG, SHA, ended, ended))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])

    checks = {"build": check(1, "build", "failure", f"https://github.com/{SLUG}/actions/runs/123/job/1")}
    if with_codecov:
        checks["codecov"] = check(2, "codecov", "failure", "https://codecov.io/gh/owner/name/commit/" + SHA)
    created = []
    monkeypatch.setattr(judging, "latest_runs", lambda slug, sha: dict(checks))
    monkeypatch.setattr(judging, "gh", lambda *args: "")
    monkeypatch.setattr(judging, "failure_log", lambda slug, sha, failed: "log")
    monkeypatch.setattr(issues, "create",
                        lambda slug, title, body, labels: created.append((title, body)) or 99)

    judging.judge_all(repo, db)
    checks["build"] = check(3, "build", build_rerun_conclusion,
                            f"https://github.com/{SLUG}/actions/runs/123/job/3")
    judging.judge_all(repo, db)
    return created


def test_follow_up_body_claims_rerun_only_for_rerun_checks(tmp_path, monkeypatch):
    # Scenario 1: build fails twice, codecov (non-Actions) was never re-run.
    created = run_scenario(tmp_path / "one", monkeypatch, "failure")
    assert len(created) == 1
    title, body = created[0]
    assert title == "CI failed on main after #42: build, codecov"
    assert "`build`" in body and "`codecov`" in body
    assert "and again when re-run: `build`, `codecov`" not in body, body
    assert "and again when re-run: `build`." in body, body

    # Scenario 2: build's re-run passes; only codecov, never re-run, still fails.
    created = run_scenario(tmp_path / "two", monkeypatch, "success")
    assert len(created) == 1
    title, body = created[0]
    assert title == "CI failed on main after #42: codecov"
    assert "`codecov`" in body
    assert "again when re-run" not in body, body


def test_follow_up_body_still_claims_rerun_for_actions_check_that_failed_twice(tmp_path, monkeypatch):
    created = run_scenario(tmp_path, monkeypatch, "failure", with_codecov=False)
    assert len(created) == 1
    title, body = created[0]
    assert title == "CI failed on main after #42: build"
    assert "and again when re-run: `build`." in body, body
    assert "not re-run" not in body, body
