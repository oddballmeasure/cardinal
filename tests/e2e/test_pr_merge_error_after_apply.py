import json

import pytest

from cardinal.contracts.verdict import Verdict
from cardinal.github import prs

SLUG = "owner/name"
BRANCH = "cardinal/issue-7"
SHA = "b" * 40
OTHER_SHA = "d" * 40
MERGE_SHA = "c" * 40


def make_service(tmp_path):
    verdict = Verdict(branch=BRANCH, head_sha=SHA, approved=True, requirements={"R1": True},
                      tests_are_e2e=True, repo_tests_passed=True, test_files_changed=True,
                      off_limits_touched=[], findings=[])
    clone = tmp_path / "clone"
    clone.mkdir()
    return prs.PRService(clone, SLUG, "main", BRANCH, verdict, required_checks=[], ci_timeout=5,
                         poll_seconds=0, closes=7)


def fake_gh(monkeypatch, after_merge, read_back_error=False):
    """`gh pr merge` reports HTTP 502; a later `pr view` of the merge fields returns after_merge
    (or raises when read_back_error) to model what GitHub actually did."""
    commands = []
    open_pr = {"number": 42, "url": f"https://github.com/{SLUG}/pull/42", "headRefOid": SHA,
               "headRefName": BRANCH, "baseRefName": "main"}
    state = {"merged": False}

    def gh_json(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("pr", "list"):
            return [] if state["merged"] else [dict(open_pr)]
        if args[:2] == ("pr", "view"):
            fields = args[-1]
            if "mergeCommit" in fields:
                if read_back_error:
                    raise prs.GhError("HTTP 502: Bad Gateway (read-back)")
                if state["merged"]:
                    return dict(after_merge)
                return {"state": "OPEN", "mergeCommit": None, "headRefOid": SHA}
            return {"headRefOid": SHA, "state": "OPEN", "mergeable": "MERGEABLE", "baseRefName": "main"}
        raise AssertionError(f"unexpected gh_json {args}")

    def gh(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("pr", "merge"):
            state["merged"] = True
            raise prs.GhError("HTTP 502: Bad Gateway; without retry (the operation may already have applied)")
        return ""

    monkeypatch.setattr(prs, "gh_json", gh_json)
    monkeypatch.setattr(prs, "gh", gh)
    return commands


def merge_calls(commands):
    return [c for c in commands if c[:2] == ("pr", "merge")]


def ready_tools(service):
    tools = {t.name: t for t in service.tools()}
    listed = json.loads(tools["list_open_prs"].invoke({}))
    assert [pr["number"] for pr in listed] == [42]
    assert json.loads(tools["wait_for_ci"].invoke({"number": 42}))["status"] == "success"
    return tools


def test_merge_that_applied_despite_gh_error_is_recorded(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, {"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA}, "headRefOid": SHA})
    service = make_service(tmp_path)
    tools = ready_tools(service)

    try:
        out = tools["merge_pull_request"].invoke({"number": 42})
    except prs.GhError as error:
        raise AssertionError(f"merge_pull_request raised although GitHub reports the PR MERGED: {error}")

    assert json.loads(out) == {"merged": True, "merge_sha": MERGE_SHA}
    assert service.result().status == "merged"
    assert service.merge_sha == MERGE_SHA
    assert {"action": "merge", "number": 42, "merge_sha": MERGE_SHA} in service.events
    assert len(merge_calls(commands)) == 1
    assert ("pr", "view", "42", "-R", SLUG, "--json", "state,mergeCommit,headRefOid") in commands
    assert service.violations == []


def test_merge_error_with_pr_still_open_is_raised(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, {"state": "OPEN"})
    service = make_service(tmp_path)
    tools = ready_tools(service)

    with pytest.raises(prs.GhError, match="HTTP 502: Bad Gateway; without retry"):
        tools["merge_pull_request"].invoke({"number": 42})

    assert service.result().status != "merged"
    assert service.merge_sha is None
    assert len(merge_calls(commands)) == 1


def test_merge_error_with_merged_pr_at_other_head_is_raised(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, {"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA}, "headRefOid": OTHER_SHA})
    service = make_service(tmp_path)
    tools = ready_tools(service)

    with pytest.raises(prs.GhError, match="HTTP 502: Bad Gateway; without retry"):
        tools["merge_pull_request"].invoke({"number": 42})

    assert service.merge_sha is None
    assert service.result().status != "merged"
    assert len(merge_calls(commands)) == 1


def test_merge_error_with_merged_pr_without_merge_commit_is_raised(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, {"state": "MERGED", "mergeCommit": None, "headRefOid": SHA})
    service = make_service(tmp_path)
    tools = ready_tools(service)

    with pytest.raises(prs.GhError, match="HTTP 502: Bad Gateway; without retry"):
        tools["merge_pull_request"].invoke({"number": 42})

    assert service.merge_sha is None
    assert len(merge_calls(commands)) == 1


def test_merge_error_with_failing_read_back_raises_original_error(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, {}, read_back_error=True)
    service = make_service(tmp_path)
    tools = ready_tools(service)

    with pytest.raises(prs.GhError, match="without retry") as raised:
        tools["merge_pull_request"].invoke({"number": 42})

    assert "read-back" not in str(raised.value)
    assert service.merge_sha is None
    assert len(merge_calls(commands)) == 1
