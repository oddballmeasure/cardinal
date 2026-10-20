"""`cardinal resume` whose label edit fails must leave the run resumable.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store. The run is left as
settle() leaves a paused run (status awaiting_human, label needs-human); the label edit to in-progress
is scripted to fail after GitHub applied it, as an ambiguous outcome would."""

import json
import os

import pytest

from cardinal import app
from cardinal.config.models import Repo
from cardinal.github.gh import GhError
from cardinal.store import runs
from cardinal.store.db import connect
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]


def _claims(db):
    return db.execute("SELECT COUNT(*) FROM claims WHERE repo = ? AND issue = ?", (SLUG, 14)).fetchone()[0]


def test_failed_resume_label_edit_leaves_run_awaiting_human(tmp_path, monkeypatch):
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "issues": {"14": {"number": 14, "title": "t", "body": "", "state": "OPEN",
                          "labels": ["cardinal:needs-human"], "comments": []}},
        "gh_failures": [{"command": ["issue", "edit"], "contains": "cardinal:in-progress",
                         "responses": [{"error": "GraphQL: Forbidden (403)", "after": True}]}]}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    db = connect(tmp_path / "home" / "store.db")
    run_id = "abcdef012345"
    runs.create(db, run_id, f"{SLUG}#14#{run_id}", SLUG, 14)
    runs.update(db, run_id, status="awaiting_human")

    with pytest.raises(GhError):
        app.resume(None, None, repo, db, 14, "approve", "")

    issue = json.loads(state_path.read_text())["issues"]["14"]
    assert _claims(db) == 0
    assert runs.get(db, run_id)["status"] == "awaiting_human", dict(runs.get(db, run_id))
    assert runs.get(db, run_id)["ended_at"] is None
    assert [name for name in issue["labels"] if name.startswith("cardinal:")] == ["cardinal:needs-human"], issue
    assert runs.latest(db, SLUG, 14)["status"] == "awaiting_human"

    # A person tries again with gh working: resume passes the status check and the claim, relabels the
    # issue, and hands the decision to the graph. The graph itself is stood in for; it settles done.
    driven = []

    def drive(run, thread_id, payload, *, daemon=False):
        driven.append((run.run_id, thread_id, payload.resume, _claims(db),
                       json.loads(state_path.read_text())["issues"]["14"]["labels"]))
        runs.update(db, run.run_id, status="done")
        return {"run_id": run.run_id, "issue": 14, "status": "done"}

    monkeypatch.setattr(app, "drive", drive)
    result = app.resume(None, None, repo, db, 14, "approve", "go ahead")

    assert result["status"] == "done"
    assert driven == [(run_id, f"{SLUG}#14#{run_id}", {"action": "approve", "note": "go ahead"}, 1,
                       ["cardinal:in-progress"])], driven
    assert _claims(db) == 0
    assert runs.get(db, run_id)["status"] == "done"
