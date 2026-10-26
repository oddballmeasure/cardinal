"""An issue the monitor created stays "filed" (and counts toward the cap) even when triage raises.

The store is a real Cardinal store fed through the real sink; drafting, `gh issue create` and triage
are stood in for so the only failure is triage raising after the issue already exists."""

from datetime import UTC, datetime
from types import SimpleNamespace

from cardinal import app
from cardinal.config.models import Monitor, Repo
from cardinal.contracts.monitor import IssueDraft
from cardinal.logs.record import LogRecord, Source
from cardinal.logs.sink import Sink
from cardinal.monitor import monitor
from cardinal.store import findings
from cardinal.store.db import connect

SLUG = "team/app"


def setup(tmp_path, monkeypatch, create):
    db = connect(tmp_path / "store.db")
    sink = Sink(tmp_path / "logs", db)
    for event in ("first_boom", "second_boom"):
        sink.write(LogRecord(at=datetime.now(UTC), level="critical", event=event, message=event,
                             source=Source(repo=SLUG, component="api")))

    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    config = SimpleNamespace(monitor=Monitor(min_occurrences=1, window_hours=24, max_issues_per_pass=1),
                             repo=lambda slug: repo)

    def triage(*args, **kwargs):
        raise RuntimeError("gh down")

    monkeypatch.setattr(monitor, "draft", lambda home, config, db, group: IssueDraft(
        title="API crashes on boom", body="The api component raises a critical boom error on every request."))
    monkeypatch.setattr(monitor.issues, "create", create)
    monkeypatch.setattr(app, "triage", triage)
    return db, config


def test_triage_error_after_create_is_filed_and_counts_toward_cap(tmp_path, monkeypatch):
    created = []

    def create(slug, title, body):
        created.append(slug)
        return 7

    db, config = setup(tmp_path, monkeypatch, create)

    results = monitor.once(None, config, db)

    assert len(created) == 1, f"issues.create called {len(created)} times with max_issues_per_pass=1: {results}"
    assert not [r for r in results if r["action"] == "failed"], results
    first, second = results
    assert (first["action"], first["issue"], first["label"]) == ("filed", 7, None), first
    assert "gh down" in first["triage_error"], first
    assert second["action"] == "held_by_cap", second
    # Only the capped group holds the cursor; the filed one is not reconsidered.
    assert findings.cursor(db) >= 1, "the cursor was held at the group whose issue was created"


def test_create_error_is_still_failed_and_holds_cursor(tmp_path, monkeypatch):
    def create(slug, title, body):
        raise RuntimeError("create down")

    db, config = setup(tmp_path, monkeypatch, create)

    results = monitor.once(None, config, db)

    assert [r["action"] for r in results] == ["failed", "failed"], results
    assert "create down" in results[0]["error"], results
    assert findings.cursor(db) == 0, "the cursor moved past groups that were never filed"
