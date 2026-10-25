"""Orphaned-claim recovery for a run whose process died after settle() recorded `done`.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store. The run is left
as settle() leaves it after recording its `run settled` event (status done) but before the claim was
released, and the claim's pid is dead. The daemon's orphan sweep (loop.drain) then runs
`app.abandon` on each orphaned claim."""

import json
import os

from cardinal import app
from cardinal.config.models import Repo
from cardinal.store import inputs, runs
from cardinal.store.db import connect, now
from cardinal.store.recorder import Recorder
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]
DEAD_PID = 999_999_999


def _world(tmp_path, monkeypatch, state="CLOSED"):
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "issues": {"14": {"number": 14, "title": "t", "body": "", "state": state,
                          "labels": ["cardinal:in-progress"], "comments": []}}}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    db = connect(tmp_path / "home" / "store.db")
    return state_path, repo, db


def _orphan(db, run_id):
    assert inputs.claim(db, SLUG, 14, run_id)
    db.execute("UPDATE claims SET pid = ? WHERE repo = ? AND issue = ?", (DEAD_PID, SLUG, 14))
    runs.create(db, run_id, f"{SLUG}#14#{run_id}", SLUG, 14)


def _sweep(db, repo, run_id):
    # The same sweep loop.drain runs at the top of every poll.
    orphans = inputs.orphaned_claims(db, SLUG)
    assert [row["run_id"] for row in orphans] == [run_id]
    return [app.abandon(repo, db, claim) for claim in orphans]


def _claims(db):
    return db.execute("SELECT COUNT(*) FROM claims WHERE repo = ? AND issue = ?", (SLUG, 14)).fetchone()[0]


def test_orphan_sweep_keeps_a_run_that_already_settled_done(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    run_id = "feedfacedone"
    _orphan(db, run_id)
    runs.update(db, run_id, status="done", ended_at=now())
    Recorder(db, run_id).event("run", "settled", {"status": "done", "failure": {}})

    results = _sweep(db, repo, run_id)

    issue = json.loads(state_path.read_text())["issues"]["14"]
    abandoned = [body for body in issue["comments"] if body.startswith("Cardinal stopped at `abandoned`")]
    assert runs.get(db, run_id)["status"] == "done", dict(runs.get(db, run_id))
    assert "cardinal:done" in issue["labels"] and "cardinal:error" not in issue["labels"], issue["labels"]
    assert abandoned == [], issue["comments"]
    assert len([body for body in issue["comments"] if body.startswith("Cardinal resolved this")]) == 1, issue["comments"]
    assert _claims(db) == 0
    assert results[0]["status"] == "done", results


def test_orphan_sweep_still_marks_an_unsettled_run_as_a_crash(tmp_path, monkeypatch):
    state_path, repo, db = _world(tmp_path, monkeypatch, state="OPEN")
    run_id = "feedfacedead"
    _orphan(db, run_id)

    results = _sweep(db, repo, run_id)

    issue = json.loads(state_path.read_text())["issues"]["14"]
    row = runs.get(db, run_id)
    assert (row["status"], row["failure_kind"]) == ("failed", "crash"), dict(row)
    assert "cardinal:error" in issue["labels"], issue["labels"]
    assert [body for body in issue["comments"] if body.startswith("Cardinal stopped at `abandoned` (crash)")], issue
    assert _claims(db) == 0
    assert results[0]["status"] == "failed" and results[0]["failure"]["kind"] == "crash", results
