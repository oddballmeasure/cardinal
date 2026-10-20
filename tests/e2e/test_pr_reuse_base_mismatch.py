import json

from cardinal.contracts.verdict import Verdict
from cardinal.github import prs

SLUG = "owner/name"
BRANCH = "cardinal/issue-7"
SHA = "b" * 40
MERGE_SHA = "c" * 40


def make_service(tmp_path):
    verdict = Verdict(branch=BRANCH, head_sha=SHA, approved=True, requirements={"R1": True},
                      tests_are_e2e=True, repo_tests_passed=True, test_files_changed=True,
                      off_limits_touched=[], findings=[])
    clone = tmp_path / "clone"
    clone.mkdir()
    return prs.PRService(clone, SLUG, "main", BRANCH, verdict, required_checks=[], ci_timeout=5,
                         poll_seconds=0, closes=7)


def fake_gh(monkeypatch, base_ref, view_base=None):
    """gh lists one open PR from BRANCH into base_ref; `pr view` reports view_base (default base_ref),
    so a test can model a PR retargeted after it was listed."""
    commands = []
    open_pr = {"number": 42, "url": f"https://github.com/{SLUG}/pull/42", "headRefOid": SHA,
               "headRefName": BRANCH, "baseRefName": base_ref}
    state = {"merged": False, "view_base": view_base or base_ref}

    def gh_json(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("pr", "list"):
            return [] if state["merged"] else [dict(open_pr)]
        if args[:2] == ("pr", "view"):
            if "state,mergeCommit" in args:
                return {"state": "MERGED", "mergeCommit": {"oid": MERGE_SHA}}
            return {"headRefOid": SHA, "state": "OPEN", "mergeable": "MERGEABLE",
                    "baseRefName": state["view_base"]}
        raise AssertionError(f"unexpected gh_json {args}")

    def gh(*args, **kwargs):
        commands.append(args)
        if args[:2] == ("pr", "merge"):
            state["merged"] = True
        return ""

    monkeypatch.setattr(prs, "gh_json", gh_json)
    monkeypatch.setattr(prs, "gh", gh)
    return commands


def run_reuse_flow(service):
    tools = {t.name: t for t in service.tools()}
    listed = json.loads(tools["list_open_prs"].invoke({}))
    # A model that trusts list_open_prs proceeds to wait for CI and merge the listed PR.
    for pr in listed:
        tools["wait_for_ci"].invoke({"number": pr["number"]})
        tools["merge_pull_request"].invoke({"number": pr["number"]})
    return listed


def test_open_pr_into_other_base_is_not_reused_or_merged(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, "develop")
    service = make_service(tmp_path)

    listed = run_reuse_flow(service)

    assert 42 not in [pr.get("number") for pr in listed if isinstance(pr, dict)], listed
    assert service.number is None, "a PR targeting develop must not be recorded as this run's PR (base is main)"
    assert not [c for c in commands if c[:2] == ("pr", "merge")], "gh pr merge ran on a PR whose base is develop"
    assert service.result().status != "merged"


def test_open_pr_into_pinned_base_is_still_reused(tmp_path, monkeypatch):
    fake_gh(monkeypatch, "main")
    service = make_service(tmp_path)
    tools = {t.name: t for t in service.tools()}

    listed = json.loads(tools["list_open_prs"].invoke({}))

    assert [pr["number"] for pr in listed] == [42]
    assert service.number == 42


def test_open_pr_into_pinned_base_still_merges(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, "main")
    service = make_service(tmp_path)

    run_reuse_flow(service)

    assert [c for c in commands if c[:2] == ("pr", "merge")]
    assert service.result().status == "merged"
    assert service.merge_sha == MERGE_SHA
    assert service.violations == []


def test_pr_retargeted_after_listing_is_not_merged(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, "main", view_base="develop")
    service = make_service(tmp_path)

    listed = run_reuse_flow(service)

    assert [pr["number"] for pr in listed] == [42]
    assert not [c for c in commands if c[:2] == ("pr", "merge")], "gh pr merge ran on a PR retargeted to develop"
    assert service.result().status != "merged"
    assert any("develop" in v for v in service.violations), service.violations


def test_create_is_not_blocked_by_pr_into_other_base(tmp_path, monkeypatch):
    commands = fake_gh(monkeypatch, "develop")
    service = make_service(tmp_path)
    tools = {t.name: t for t in service.tools()}

    json.loads(tools["list_open_prs"].invoke({}))
    try:
        tools["create_pull_request"].invoke({"title": "Fix", "body": "Body"})
    except prs.GhError:
        pass  # the fake lists no PR into main after creating one; only the create attempt matters here

    assert [c for c in commands if c[:2] == ("pr", "create")], "create_pull_request was blocked by a PR into develop"
    assert not any("already open" in v for v in service.violations), service.violations
