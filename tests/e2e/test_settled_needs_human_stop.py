"""A daemon stop landing after settle() set the needs-human label for an `awaiting_human` or
`rejected` run but before it posted the question / rejection comment.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store. settle() runs
for real and the stop (KeyboardInterrupt) is raised from its comment call, then the daemon's stop
path, `app.interrupt`, runs."""

import json
import os
from types import SimpleNamespace

import pytest

from cardinal import app
from cardinal.config.models import Repo
from cardinal.github import issues
from cardinal.store import runs
from cardinal.store.db import connect
from cardinal.store.recorder import Recorder
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]
QUESTION = "Cardinal needs a decision before continuing"
REJECTED = "Cardinal is not taking this issue:"


def _world(tmp_path, monkeypatch):
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "issues": {str(n): {"number": n, "title": "t", "body": "", "state": "OPEN",
                            "labels": ["cardinal:in-progress"], "comments": []} for n in (14, 15)}}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    db = connect(tmp_path / "home" / "store.db")
    return state_path, repo, db


def _issue(state_path, number):
    return json.loads(state_path.read_text())["issues"][str(number)]


def _run(repo, db, run_id, number):
    runs.create(db, run_id, f"{SLUG}#{number}#{run_id}", SLUG, number)
    return SimpleNamespace(repo=repo, issue=SimpleNamespace(number=number), db=db, run_id=run_id,
                           recorder=Recorder(db, run_id), branch=f"cardinal/{number}")


def _settle_stopped_before_comment(monkeypatch, repo, db, run_id, number, values, paused):
    run = _run(repo, db, run_id, number)
    real_comment = issues.comment

    def stopped(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(issues, "comment", stopped)
    with pytest.raises(KeyboardInterrupt):
        app.settle(run, values, paused)
    monkeypatch.setattr(issues, "comment", real_comment)


def _count(state_path, number, prefix):
    return [body for body in _issue(state_path, number)["comments"] if body.startswith(prefix)]


def test_stop_after_needs_human_settle_posts_the_missing_question_and_rejection_once(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)

    asked = "feedface0014"
    _settle_stopped_before_comment(monkeypatch, repo, db, asked, 14,
                                   {"status": "running", "question": "Ship the risky migration?"}, True)
    rejected = "feedface0015"
    _settle_stopped_before_comment(monkeypatch, repo, db, rejected, 15,
                                   {"status": "rejected", "decision": {"reason": "This is a feature request."}},
                                   False)
    assert _issue(state_path, 14)["labels"] == ["cardinal:needs-human"]
    assert _issue(state_path, 14)["comments"] == []
    assert _issue(state_path, 15)["comments"] == []

    app.interrupt(repo, db, asked, 14)
    app.interrupt(repo, db, rejected, 15)

    question = _count(state_path, 14, QUESTION)
    assert len(question) == 1, _issue(state_path, 14)["comments"]
    assert "Ship the risky migration?" in question[0], question
    assert "cardinal resume 14 --approve" in question[0], question
    assert _issue(state_path, 14)["labels"] == ["cardinal:needs-human"]

    reason = _count(state_path, 15, REJECTED)
    assert reason == [f"{REJECTED} This is a feature request."], _issue(state_path, 15)["comments"]
    assert _issue(state_path, 15)["labels"] == ["cardinal:needs-human"]

    # A second stop finds the comments and posts no others.
    app.interrupt(repo, db, asked, 14)
    app.interrupt(repo, db, rejected, 15)
    assert len(_issue(state_path, 14)["comments"]) == 1, _issue(state_path, 14)["comments"]
    assert len(_issue(state_path, 15)["comments"]) == 1, _issue(state_path, 15)["comments"]
    assert runs.get(db, asked)["status"] == "awaiting_human"
    assert runs.get(db, rejected)["status"] == "rejected"


def test_stop_after_settle_already_commented_posts_no_duplicate(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)

    asked, rejected = "decafbad0014", "decafbad0015"
    app.settle(_run(repo, db, asked, 14), {"status": "running", "question": "Which API?"}, True)
    app.settle(_run(repo, db, rejected, 15), {"status": "rejected", "decision": {"reason": "Out of scope."}}, False)
    posted_question = _issue(state_path, 14)["comments"]
    posted_reason = _issue(state_path, 15)["comments"]
    assert len(posted_question) == 1 and posted_question[0].startswith(QUESTION), posted_question
    assert posted_reason == [f"{REJECTED} Out of scope."], posted_reason

    app.interrupt(repo, db, asked, 14)
    app.interrupt(repo, db, rejected, 15)

    assert _issue(state_path, 14)["comments"] == posted_question
    assert _issue(state_path, 15)["comments"] == posted_reason
    assert _issue(state_path, 14)["labels"] == ["cardinal:needs-human"]
    assert _issue(state_path, 15)["labels"] == ["cardinal:needs-human"]
    assert runs.get(db, asked)["status"] == "awaiting_human"
    assert runs.get(db, rejected)["status"] == "rejected"
