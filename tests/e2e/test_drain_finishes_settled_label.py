"""settle() recorded a `failed` run, then its label edit raised, so the claim was released with the issue
still labelled working and no "Cardinal stopped at" comment. The next daemon poll, `loop.drain`, must
finish the label and comment the way `app.finish_settled` does.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store."""

import json
import os

from cardinal.config.models import Repo
from cardinal.daemon import loop
from cardinal.store import inputs, runs
from cardinal.store.db import connect, now
from cardinal.store.recorder import Recorder
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]


def _world(tmp_path, monkeypatch):
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    state_path = tmp_path / "gh-state.json"
    # As settle() leaves it when set_state raised: still labelled working, no comment, claim released.
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "issues": {"14": {"number": 14, "title": "t", "body": "", "state": "OPEN",
                          "labels": [repo.labels.working], "comments": []}}}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    db = connect(tmp_path / "home" / "store.db")
    return state_path, repo, db


def _settle_failed(db, run_id):
    failure = {"kind": "tests_failed", "stage": "code", "detail": "the suite failed"}
    runs.create(db, run_id, f"{SLUG}#14#{run_id}", SLUG, 14)
    runs.update(db, run_id, status="failed", failure_kind=failure["kind"], failure_detail=failure["detail"],
                ended_at=now())
    Recorder(db, run_id).event("run", "settled", {"status": "failed", "failure": failure})


def _issue(state_path):
    return json.loads(state_path.read_text())["issues"]["14"]


def _state_labels(issue):
    return [name for name in issue["labels"] if name in LABELS]


def test_drain_finishes_label_and_comment_of_failed_run_whose_settle_label_edit_failed(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacecafe"
    _settle_failed(db, run_id)

    loop.drain(None, None, repo, db)

    issue = _issue(state_path)
    stopped = [body for body in issue["comments"] if body.startswith("Cardinal stopped at")]
    assert _state_labels(issue) == [repo.labels.error], issue
    assert len(stopped) == 1, issue["comments"]
    assert runs.get(db, run_id)["status"] == "failed"


def test_drain_twice_posts_the_stopped_comment_once(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacecafe"
    _settle_failed(db, run_id)

    loop.drain(None, None, repo, db)
    loop.drain(None, None, repo, db)

    issue = _issue(state_path)
    stopped = [body for body in issue["comments"] if body.startswith("Cardinal stopped at")]
    assert _state_labels(issue) == [repo.labels.error], issue
    assert len(stopped) == 1, issue["comments"]
    assert runs.get(db, run_id)["status"] == "failed"


def test_drain_finishes_label_and_comment_of_done_run_whose_settle_label_edit_failed(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacedone"
    runs.create(db, run_id, f"{SLUG}#14#{run_id}", SLUG, 14)
    runs.update(db, run_id, status="done", pr_url="https://github.invalid/team/app/pull/100",
                merge_sha="0123456789abcdef", ended_at=now())
    Recorder(db, run_id).event("run", "settled", {"status": "done", "failure": {}})

    loop.drain(None, None, repo, db)

    issue = _issue(state_path)
    resolved = [body for body in issue["comments"] if body.startswith("Cardinal resolved this")]
    assert _state_labels(issue) == [repo.labels.done], issue
    assert len(resolved) == 1, issue["comments"]
    assert runs.get(db, run_id)["status"] == "done"


def test_drain_leaves_a_working_issue_with_a_live_claim_alone(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacelive"
    _settle_failed(db, run_id)
    assert inputs.claim(db, SLUG, 14, run_id)  # held by this (live) process
    before = _issue(state_path)

    loop.drain(None, None, repo, db)

    state = json.loads(state_path.read_text())
    issue = state["issues"]["14"]
    assert issue["labels"] == before["labels"] == [repo.labels.working], issue
    assert issue["comments"] == [], issue["comments"]
    assert [edit for edit in state.get("edit_labels", []) if edit["issue"] == "14"] == []
    assert runs.get(db, run_id)["status"] == "failed"
