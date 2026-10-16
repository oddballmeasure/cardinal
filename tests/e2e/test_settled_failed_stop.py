"""A daemon stop landing after settle() recorded a `failed` run but before its comment was posted.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store. The run is left
exactly as settle() leaves it after setting the error label (app.py) and before it comments, then the
daemon's stop path, `app.interrupt`, runs."""

import json
import os

from cardinal import app
from cardinal.config.models import Repo
from cardinal.store import runs
from cardinal.store.db import connect, now
from cardinal.store.recorder import Recorder
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]


def _world(tmp_path, monkeypatch, comments=()):
    state_path = tmp_path / "gh-state.json"
    # settle() has already moved the label to error; the "Cardinal stopped at" comment never went out.
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "issues": {"14": {"number": 14, "title": "t", "body": "", "state": "OPEN",
                          "labels": ["cardinal:error"], "comments": list(comments)}}}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    db = connect(tmp_path / "home" / "store.db")
    return state_path, repo, db


def _failed_run(db, run_id, failure, *, event=True):
    runs.create(db, run_id, f"{SLUG}#14#{run_id}", SLUG, 14)
    runs.update(db, run_id, status="failed", failure_kind=failure["kind"], failure_detail=failure["detail"],
                ended_at=now())
    if event:
        Recorder(db, run_id).event("run", "settled", {"status": "failed", "failure": failure})


def _issue(state_path):
    return json.loads(state_path.read_text())["issues"]["14"]


def test_stop_after_failed_settle_posts_the_missing_stopped_at_comment_once(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacecafe"
    failure = {"kind": "verification", "stage": "verify", "detail": "the requirement is not covered"}
    _failed_run(db, run_id, failure)

    app.interrupt(repo, db, run_id, 14)

    issue = _issue(state_path)
    stopped = [body for body in issue["comments"] if body.startswith("Cardinal stopped at")]
    assert issue["labels"] == ["cardinal:error"], issue
    assert runs.get(db, run_id)["status"] == "failed"
    assert len(stopped) == 1, issue["comments"]
    assert stopped[0] == "Cardinal stopped at `verify` (verification): the requirement is not covered", stopped

    # A second stop finds the comment and posts no other.
    app.interrupt(repo, db, run_id, 14)
    issue = _issue(state_path)
    assert len(issue["comments"]) == 1, issue["comments"]
    assert issue["labels"] == ["cardinal:error"], issue
    assert runs.get(db, run_id)["status"] == "failed"


def test_stop_after_failed_settle_does_not_duplicate_an_existing_stopped_at_comment(tmp_path, monkeypatch):
    existing = "Cardinal stopped at `verify` (verification): already said"
    state_path, repo, db = _world(tmp_path, monkeypatch, comments=[existing])
    run_id = "decafbad0001"
    _failed_run(db, run_id, {"kind": "verification", "stage": "verify", "detail": "x"})

    app.interrupt(repo, db, run_id, 14)

    issue = _issue(state_path)
    assert issue["comments"] == [existing], issue["comments"]
    assert issue["labels"] == ["cardinal:error"], issue
    assert runs.get(db, run_id)["status"] == "failed"


def test_stop_after_failed_settle_hides_provider_detail(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "decafbad0002"
    _failed_run(db, run_id, {"kind": "provider", "stage": "plan", "detail": "secret provider error"})

    app.interrupt(repo, db, run_id, 14)

    assert _issue(state_path)["comments"] == ["Cardinal stopped at `plan` (provider)"]
