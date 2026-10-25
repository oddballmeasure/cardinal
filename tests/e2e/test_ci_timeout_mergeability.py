"""A "timeout" whose required checks had passed, and only GitHub's mergeable state stalled, must not be
reported as slow checks. `land` runs with the real PRService tools against a fake gh; run_agent is
stubbed to call those tools the way the pr-manager agent would."""

import json
from types import SimpleNamespace

import pytest

from cardinal.contracts.verdict import Verdict
from cardinal.github import prs
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.roles import pr_manager

SLUG = "owner/name"
BRANCH = "cardinal/75-ci-timeout"
SHA = "b" * 40
CHECK = "tests"


class Recorder:
    def __init__(self):
        self.events = []

    def event(self, *args):
        self.events.append(args)


def make_run(tmp_path, required_checks, ci_timeout=0):
    clone = tmp_path / "clone"
    clone.mkdir()
    context_dir = tmp_path / "context"
    context_dir.mkdir()
    limits = SimpleNamespace(ci_timeout_seconds=ci_timeout, ci_poll_seconds=0, recursion_limit=50,
                             model_timeout_seconds=60)
    config = SimpleNamespace(limits=limits, models=SimpleNamespace(pr_manager="fake:model"), ceiling=None)
    repo = SimpleNamespace(slug=SLUG, base_branch="main", required_checks=required_checks)
    issue = SimpleNamespace(number=75, model_dump=lambda: {"number": 75})
    return SimpleNamespace(
        config=config, repo=repo, issue=issue, clone=clone, branch=BRANCH, context_dir=context_dir,
        recorder=Recorder(), write_context=lambda name, value: None,
        agent_context=lambda **extra: dict(extra))


def verdict():
    return Verdict(branch=BRANCH, head_sha=SHA, approved=True, requirements={"R1": True},
                   tests_are_e2e=True, repo_tests_passed=True, test_files_changed=True,
                   off_limits_touched=[], findings=[])


def fake_gh(monkeypatch, mergeable, check_status="completed", check_conclusion="success"):
    """One open PR #42 into main. `mergeable(n)` gives the mergeable value of the n-th `pr view`."""
    views = {"n": 0}
    commands = []

    def gh_json(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("pr", "list"):
            return [{"number": 42, "url": f"https://github.com/{SLUG}/pull/42", "headRefOid": SHA,
                     "headRefName": BRANCH, "baseRefName": "main"}]
        if args[:2] == ("pr", "view"):
            views["n"] += 1
            return {"headRefOid": SHA, "state": "OPEN", "mergeable": mergeable(views["n"]), "baseRefName": "main"}
        if args[0] == "api" and "check-runs" in args[1]:
            return {"check_runs": [{"id": 1, "name": CHECK, "head_sha": SHA, "status": check_status,
                                    "conclusion": check_conclusion if check_status == "completed" else None}]}
        raise AssertionError(f"unexpected gh_json {args}")

    def gh(*args, **kwargs):
        commands.append(args)
        return ""

    monkeypatch.setattr(prs, "gh_json", gh_json)
    monkeypatch.setattr(prs, "gh", gh)
    return commands


def agent_reuses_waits_and_merges(monkeypatch):
    def run_agent(call, *args, **kwargs):
        tools = {t.name: t for t in call.tools}
        listed = json.loads(tools["list_open_prs"].invoke({}))
        number = listed[0]["number"]
        status = json.loads(tools["wait_for_ci"].invoke({"number": number}))["status"]
        if status == "success":
            tools["merge_pull_request"].invoke({"number": number})

    monkeypatch.setattr(pr_manager, "run_agent", run_agent)


def land_failure(run):
    with pytest.raises(StageFailure) as caught:
        pr_manager.land(run, verdict())
    return caught.value


def test_checks_passed_but_mergeable_unknown_names_mergeability(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, lambda n: "UNKNOWN")
    agent_reuses_waits_and_merges(monkeypatch)

    failure = land_failure(make_run(tmp_path, [CHECK]))

    assert failure.kind == FailureKind.CI_TIMEOUT
    assert "UNKNOWN" in failure.detail, failure.detail
    assert "MERGEABLE" in failure.detail
    assert "Required checks did not finish" not in failure.detail, failure.detail
    assert not [c for c in commands if c[:2] == ("pr", "merge")]


def test_no_required_checks_and_mergeable_unknown_names_mergeability(tmp_path, monkeypatch):
    fake_gh(monkeypatch, lambda n: "UNKNOWN")
    agent_reuses_waits_and_merges(monkeypatch)

    failure = land_failure(make_run(tmp_path, []))

    assert failure.kind == FailureKind.CI_TIMEOUT
    assert "UNKNOWN" in failure.detail, failure.detail
    assert "Required checks did not finish" not in failure.detail, failure.detail


def test_merge_poll_timeout_after_ci_success_names_last_mergeable(tmp_path, monkeypatch):
    # wait_for_ci sees MERGEABLE; mergeability then regresses to UNKNOWN under the merge lock.
    commands = fake_gh(monkeypatch, lambda n: "MERGEABLE" if n == 1 else "UNKNOWN")
    agent_reuses_waits_and_merges(monkeypatch)

    failure = land_failure(make_run(tmp_path, [CHECK]))

    assert failure.kind == FailureKind.CI_TIMEOUT
    assert "UNKNOWN" in failure.detail, failure.detail
    assert "Required checks did not finish" not in failure.detail, failure.detail
    assert not [c for c in commands if c[:2] == ("pr", "merge")]


def test_check_in_progress_until_deadline_keeps_checks_message(tmp_path, monkeypatch):
    fake_gh(monkeypatch, lambda n: "MERGEABLE", check_status="in_progress")
    agent_reuses_waits_and_merges(monkeypatch)

    failure = land_failure(make_run(tmp_path, [CHECK], ci_timeout=0))

    assert failure.kind == FailureKind.CI_TIMEOUT
    assert failure.detail == "Required checks did not finish within 0s"


def test_check_in_progress_with_unknown_mergeable_keeps_checks_message(tmp_path, monkeypatch):
    fake_gh(monkeypatch, lambda n: "UNKNOWN", check_status="in_progress")
    agent_reuses_waits_and_merges(monkeypatch)

    failure = land_failure(make_run(tmp_path, [CHECK], ci_timeout=0))

    assert failure.kind == FailureKind.CI_TIMEOUT
    assert failure.detail == "Required checks did not finish within 0s"
