"""A scout pass must keep /context/issues.json current as it files issues.

The pass runs over a real Cardinal store; git, GitHub and the model roles are replaced by stubs that
record the issues.json each reviewer and survey would read at the moment it is called."""

import contextlib
import json
from types import SimpleNamespace

from cardinal.config.models import Repo
from cardinal.contracts.scout import Area, Evidence, Proposal, ProposalReview, ScoutFindings
from cardinal.graph.context import ScoutRun
from cardinal.home import Home
from cardinal.scout import once
from cardinal.store.db import connect
from cardinal.store.recorder import Recorder

SLUG = "team/app"


def _proposal(title: str) -> Proposal:
    evidence = Evidence(path="src/app.py", line_start=1, line_end=1, quote="def handler(): return 1")
    return Proposal(category="bug", title=title, problem="The handler returns 1 where callers expect a status of 200.",
                    evidence=[evidence], expected="It returns 200.", acceptance=["GET / returns status 200"],
                    files=["src/app.py"])


def test_reviewer_and_later_survey_read_issues_json_with_issues_filed_earlier_in_the_pass(tmp_path, monkeypatch):
    home = Home(tmp_path / "home")
    db = connect(home.store)
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[])
    settings = SimpleNamespace(autonomy="propose", categories=["bug", "feature"], max_files=3, cooldown_days=0,
                               areas_per_pass=2, max_proposals_per_pass=5, repro=False)
    config = SimpleNamespace(scout=settings, limits=SimpleNamespace(test_timeout_seconds=60))
    run = ScoutRun(home, config, repo, db, Recorder(db, "scout-test"), "scout-test", tmp_path / "worktree")

    def read_issues():
        return json.loads((run.context_dir / "issues.json").read_text())

    start = [{"number": 7, "title": "An older open issue about logging", "state": "OPEN"}]
    first = _proposal("Area one handler returns the wrong status code")
    second = _proposal("Parser drops trailing whitespace in quoted config values")
    areas = [Area(name="one", paths=["src/one"], focus="bugs"), Area(name="two", paths=["src/two"], focus="bugs")]
    surveys_saw, reviewers_saw, numbers = {}, [], iter([60, 61])

    def survey(_run, area):
        surveys_saw[area.name] = read_issues()
        return ScoutFindings(proposals=[first] if area.name == "one" else [second])

    def review(_run, proposal):
        reviewers_saw.append((proposal.title, read_issues()))
        return ProposalReview(verdict="confirmed", reason="Re-derived from the code.", evidence=proposal.evidence)

    monkeypatch.setattr(once.outcomes, "track", lambda *a, **k: [])
    monkeypatch.setattr(once.outcomes, "track_record", lambda *a, **k: {})
    monkeypatch.setattr(once.issues, "ensure_labels", lambda *a, **k: None)
    monkeypatch.setattr(once.issues, "ensure_type_labels", lambda *a, **k: None)
    monkeypatch.setattr(once.issues, "titles", lambda slug: [dict(item) for item in start])
    monkeypatch.setattr(once.issues, "create", lambda *a, **k: next(numbers))
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
    monkeypatch.setattr(once.scout, "survey", survey)
    monkeypatch.setattr(once.scout_reviewer, "review", review)
    monkeypatch.setattr(once, "proposal_problem", lambda *a, **k: None)
    monkeypatch.setattr(once, "evidence_problem", lambda *a, **k: None)
    monkeypatch.setattr(once.gate, "decide", lambda *a, **k: ("proposed", ""))
    monkeypatch.setattr(once.render, "body", lambda *a, **k: "body")

    report = once.Pass(run).execute()

    assert [item["action"] for item in report["proposals"]] == ["filed", "filed"], report["proposals"]
    assert report["areas"] == ["one", "two"]

    # Area 1 filed #60; the area-2 survey must see it: exactly one more entry than at the start.
    assert len(surveys_saw["two"]) == len(start) + 1, surveys_saw["two"]
    assert {"number": 60, "title": first.title, "state": "OPEN"} in surveys_saw["two"], surveys_saw["two"]

    # The next reviewer (for area 2's proposal) must judge against issues.json including #60.
    title, seen = reviewers_saw[1]
    assert title == second.title
    assert any(item["number"] == 60 and item["state"] == "OPEN" for item in seen), seen
