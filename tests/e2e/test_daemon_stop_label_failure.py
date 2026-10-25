"""A daemon stop (SIGTERM -> SystemExit(143)) while a serial daemon runs an issue in-process, where
restoring the ready label in `app.interrupt` fails. The stop must still win.

GitHub is the fake `gh` over a JSON state file; the store is a real Cardinal store. `app.drive` is
replaced by the stop itself, as `terminate` raises it from the signal handler mid-run."""

import json
import logging
import os

import pytest

from cardinal import app
from cardinal.config.models import Repo
from cardinal.store import runs
from cardinal.store.db import connect
from cardinal_harness import fake_gh

SLUG = "team/app"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error",
          "cardinal:needs-human", "cardinal:investigate", "cardinal:proposed"]


def _world(tmp_path, monkeypatch, gh_failures=()):
    state_path = tmp_path / "gh-state.json"
    state_path.write_text(json.dumps({
        "repository": SLUG, "labels": LABELS, "prs": [], "next_pr": 100,
        "gh_failures": list(gh_failures),
        "issues": {"14": {"number": 14, "title": "t", "body": "", "state": "OPEN",
                          "labels": ["cardinal:ready"], "comments": []}}}))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    db = connect(tmp_path / "home" / "store.db")

    def stopped(run, thread_id, payload, *, daemon=False):
        raise SystemExit(143)

    monkeypatch.setattr(app, "drive", stopped)
    return state_path, repo, db


def _claims(db):
    return db.execute("SELECT * FROM claims WHERE repo = ? AND issue = ?", (SLUG, 14)).fetchall()


def test_daemon_stop_survives_failed_ready_label_restore(tmp_path, monkeypatch, caplog):
    # Only the restore edit (adding cardinal:ready back) fails, permanently, as a 403 would.
    failures = [{"command": ["issue", "edit"], "contains": "--add-label cardinal:ready",
                 "responses": [{"error": "HTTP 403: Resource not accessible by integration (Forbidden)"}]}]
    state_path, repo, db = _world(tmp_path, monkeypatch, failures)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as stop:
        app.start(None, None, repo, db, 14, daemon=True)

    assert stop.value.code == 143
    row = runs.latest(db, SLUG, 14)
    assert row["status"] == "interrupted"
    assert len(_claims(db)) == 1, "the claim must be kept when the ready label could not be restored"
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "interrupt_label_recovery_failed" in events, events
    issue = json.loads(state_path.read_text())["issues"]["14"]
    assert issue["labels"] == ["cardinal:in-progress"], issue


def test_daemon_stop_restores_ready_label_and_releases_claim(tmp_path, monkeypatch, caplog):
    state_path, repo, db = _world(tmp_path, monkeypatch)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(SystemExit) as stop:
        app.start(None, None, repo, db, 14, daemon=True)

    assert stop.value.code == 143
    assert json.loads(state_path.read_text())["issues"]["14"]["labels"] == ["cardinal:ready"]
    assert runs.latest(db, SLUG, 14)["status"] == "interrupted"
    assert _claims(db) == []
    events = [getattr(record, "event", None) for record in caplog.records]
    assert "interrupt_label_recovery_failed" not in events, events
