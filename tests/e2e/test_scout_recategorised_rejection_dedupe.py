"""A proposal the reviewer recategorised, filed and then rejected, must not be re-filed next pass.

Two scout passes run over a real Cardinal store; git, GitHub and the model roles are stubs. The
survey proposes the same "enhancement" both times; the reviewer recategorises it to "bug"."""

import contextlib
from types import SimpleNamespace

from cardinal.config.models import Repo
from cardinal.contracts.scout import Area, Evidence, Proposal, ProposalReview, ScoutFindings
from cardinal.graph.context import ScoutRun
from cardinal.home import Home
from cardinal.scout import once
from cardinal.scout.checks import fingerprint
from cardinal.store import scout as store
from cardinal.store.db import connect
from cardinal.store.recorder import Recorder

SLUG = "team/app"


def test_rejected_recategorised_proposal_is_dropped_when_surveyed_again(tmp_path, monkeypatch):
    home = Home(tmp_path / "home")
    db = connect(home.store)
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    settings = SimpleNamespace(autonomy="propose", categories=["bug", "enhancement"], max_files=3, cooldown_days=0,
                               areas_per_pass=1, max_proposals_per_pass=5, repro=False)
    config = SimpleNamespace(scout=settings, limits=SimpleNamespace(test_timeout_seconds=60))

    evidence = Evidence(path="src/app.py", line_start=1, line_end=1, quote="def handler(): return 1")
    surveyed = Proposal(category="enhancement", title="Handler returns the wrong status code",
                        problem="The handler returns 1 where callers expect a status of 200.",
                        evidence=[evidence], expected="It returns 200.", acceptance=["GET / returns status 200"],
                        files=["src/app.py"])
    areas = [Area(name="one", paths=["src/one"], focus="bugs")]
    open_issues: list[dict] = []
    reviews = []

    def review(_run, proposal):
        reviews.append(proposal.title)
        return ProposalReview(verdict="confirmed", reason="Re-derived from the code.", evidence=proposal.evidence,
                              category="bug")

    monkeypatch.setattr(once.outcomes, "track", lambda *a, **k: [])
    monkeypatch.setattr(once.outcomes, "track_record", lambda *a, **k: {})
    monkeypatch.setattr(once.issues, "ensure_labels", lambda *a, **k: None)
    monkeypatch.setattr(once.issues, "ensure_type_labels", lambda *a, **k: None)
    monkeypatch.setattr(once.issues, "titles", lambda slug: [dict(item) for item in open_issues])
    monkeypatch.setattr(once.issues, "create", lambda *a, **k: 60)
    monkeypatch.setattr(once, "ensure_clone", lambda *a, **k: None)
    monkeypatch.setattr(once, "fetch_base", lambda *a, **k: "abc123")
    monkeypatch.setattr(once, "detached_worktree", lambda *a, **k: None)
    monkeypatch.setattr(once, "remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr(once, "lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(once.profiler, "profile", lambda *a, **k: {})
    monkeypatch.setattr(once.profiler, "tracked_files", lambda *a, **k: ["src/app.py"])
    monkeypatch.setattr(once.profiler, "dump", lambda profile: {})
    monkeypatch.setattr(once.store, "held", lambda *a, **k: [])
    monkeypatch.setattr(once.scout_planner, "plan", lambda *a, **k: areas)
    monkeypatch.setattr(once.scout, "survey", lambda _run, area: ScoutFindings(proposals=[surveyed]))
    monkeypatch.setattr(once.scout_reviewer, "review", review)
    monkeypatch.setattr(once, "proposal_problem", lambda *a, **k: None)
    monkeypatch.setattr(once, "evidence_problem", lambda *a, **k: None)
    monkeypatch.setattr(once.gate, "decide", lambda *a, **k: ("proposed", ""))
    monkeypatch.setattr(once.render, "body", lambda *a, **k: "body")

    def run_pass(pass_id):
        run = ScoutRun(home, config, repo, db, Recorder(db, pass_id), pass_id, tmp_path / f"worktree-{pass_id}")
        return once.Pass(run).execute()

    first = run_pass("scout-one")
    assert [item["action"] for item in first["proposals"]] == ["filed"], first["proposals"]

    row = store.latest_filed(db, SLUG, fingerprint(surveyed))
    assert row is not None, "filed row is not stored under the fingerprint of the proposal as surveyed"

    store.set_outcome(db, row["id"], "rejected", "not wanted")
    open_issues.append({"number": 60, "title": surveyed.title, "state": "CLOSED"})

    second = run_pass("scout-two")
    assert [item["action"] for item in second["proposals"]] == ["dropped"], second["proposals"]
    reason = second["proposals"][0]["reason"]
    assert reason.startswith("already filed as #") and "(rejected)" in reason, reason
    assert len(reviews) == 1, reviews
