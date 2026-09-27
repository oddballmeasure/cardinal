"""A stand-in for the `gh` CLI over a real bare Git remote, for offline end-to-end runs.

Modelled on gh's documented commands and JSON fields, not on what Cardinal happens to call
(lessons/fakes-model-the-vendor.md). Only commands Cardinal also runs against real GitHub in the
live suite are accepted; anything else exits nonzero so a new call cannot pass offline unnoticed.
Branch heads are read from the bare remote on every call, as GitHub would report them.

State lives in the JSON file named by CARDINAL_FAKE_GH_STATE:
  repository, bare, issues{number: {...}}, labels[], prs[], next_pr,
  ci{"script": ["pending", "success"], "check": "name"}, check_calls{sha: n}
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def branch_sha(state: dict, branch: str) -> str | None:
    try:
        return git("--git-dir", state["bare"], "rev-parse", f"refs/heads/{branch}")
    except subprocess.CalledProcessError:
        return None


def option(args: list[str], name: str, default: str | None = None) -> str | None:
    return args[args.index(name) + 1] if name in args else default


def fields(item: dict, args: list[str]) -> dict:
    wanted = option(args, "--json")
    return {key: item.get(key) for key in wanted.split(",")} if wanted else item


def issue_json(issue: dict) -> dict:
    return {**issue, "labels": [{"name": name} for name in issue["labels"]]}


def pr_json(state: dict, pr: dict) -> dict:
    sha = branch_sha(state, pr["headRefName"]) if pr["state"] == "OPEN" else pr["headRefOid"]
    return {**pr, "headRefOid": sha}


def issues(state: dict, args: list[str]):
    verb = args[0]
    if verb == "view":
        return fields(issue_json(state["issues"][args[1]]), args)
    if verb == "list":
        wanted_state = option(args, "--state", "open").upper()
        label = option(args, "--label")
        found = [issue_json(item) for item in state["issues"].values()
                 if (wanted_state == "ALL" or item["state"] == wanted_state) and (label is None or label in item["labels"])]
        return [fields(item, args) for item in sorted(found, key=lambda item: -item["number"])]
    if verb == "edit":
        issue = state["issues"][args[1]]
        for name in (option(args, "--remove-label") or "").split(","):
            if name in issue["labels"]:
                issue["labels"].remove(name)
        for name in (option(args, "--add-label") or "").split(","):
            if name:
                if name not in state["labels"]:
                    raise SystemExit(f"could not add label: '{name}' not found")
                if name not in issue["labels"]:
                    issue["labels"].append(name)
        return None
    if verb == "comment":
        state["issues"][args[1]]["comments"].append(option(args, "--body"))
        return None
    raise SystemExit(f"fake gh: unsupported issue command {args}")


def pulls(state: dict, args: list[str]):
    verb = args[0]
    if verb == "list":
        head = option(args, "--head")
        found = [pr_json(state, pr) for pr in state["prs"]
                 if pr["state"] == "OPEN" and (head is None or pr["headRefName"] == head)]
        return [fields(item, args) for item in found]
    if verb == "view":
        return fields(pr_json(state, next(pr for pr in state["prs"] if str(pr["number"]) == args[1])), args)
    if verb == "create":
        head, base = option(args, "--head"), option(args, "--base")
        if branch_sha(state, head) is None:
            raise SystemExit(f"pull request create failed: head branch {head} not found")
        if any(pr["state"] == "OPEN" and pr["headRefName"] == head for pr in state["prs"]):
            raise SystemExit(f"a pull request for branch \"{head}\" already exists")
        number = state["next_pr"]
        state["next_pr"] += 1
        url = f"https://github.invalid/{state['repository']}/pull/{number}"
        state["prs"].append({"number": number, "url": url, "headRefName": head, "baseRefName": base,
                             "headRefOid": None, "state": "OPEN", "title": option(args, "--title"),
                             "body": option(args, "--body")})
        return url
    if verb == "merge":
        pr = next(pr for pr in state["prs"] if str(pr["number"]) == args[1])
        expected = option(args, "--match-head-commit")
        current = branch_sha(state, pr["headRefName"])
        if pr["state"] != "OPEN" or (expected and expected != current):
            raise SystemExit("merge refused: head commit does not match or PR is not open")
        with tempfile.TemporaryDirectory() as work:
            git("clone", "-q", "--branch", pr["baseRefName"], state["bare"], work)
            git("-c", "user.name=Fake GitHub", "-c", "user.email=fake@github.invalid", "merge", "--no-ff", "-q",
                "-m", f"Merge pull request #{pr['number']}", f"origin/{pr['headRefName']}", cwd=work)
            git("push", "-q", "origin", f"HEAD:refs/heads/{pr['baseRefName']}", cwd=work)
        pr.update(state="MERGED", headRefOid=current)
        body = (pr.get("body") or "").lower()
        for number, issue in state["issues"].items():
            if f"closes #{number}" in body:
                issue["state"] = "CLOSED"
        return None
    raise SystemExit(f"fake gh: unsupported pr command {args}")


def api(state: dict, args: list[str]):
    path = args[0].split("?")[0]
    parts = path.split("/")
    if len(parts) == 6 and parts[0] == "repos" and parts[3] == "commits" and parts[5] == "check-runs":
        sha = parts[4]
        calls = state["check_calls"].get(sha, 0)
        state["check_calls"][sha] = calls + 1
        script = state["ci"]["script"]
        outcome = script[min(calls, len(script) - 1)]
        if outcome == "missing":
            return {"total_count": 0, "check_runs": []}
        return {"total_count": 1, "check_runs": [{
            "id": calls + 1, "name": state["ci"]["check"], "head_sha": sha,
            "status": "in_progress" if outcome == "pending" else "completed",
            "conclusion": None if outcome == "pending" else outcome}]}
    raise SystemExit(f"fake gh: unsupported api path {path}")


def main() -> None:
    path = Path(os.environ["CARDINAL_FAKE_GH_STATE"])
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    if "-R" in args:
        index = args.index("-R")
        if args[index + 1] != state["repository"]:
            raise SystemExit(f"GraphQL: Could not resolve to a Repository with the name '{args[index + 1]}'.")
        args = args[:index] + args[index + 2:]
    log = state.setdefault("calls", [])
    log.append(sys.argv[1:])
    if args[:2] == ["auth", "status"]:
        result = "Logged in to github.invalid as fake"
    elif args[:2] == ["repo", "view"]:
        result = {"nameWithOwner": args[2]}
    elif args[:2] == ["label", "create"]:
        if args[2] not in state["labels"]:
            state["labels"].append(args[2])
        result = None
    elif args[0] == "issue":
        result = issues(state, args[1:])
    elif args[0] == "pr":
        result = pulls(state, args[1:])
    elif args[0] == "api":
        result = api(state, args[1:])
    else:
        raise SystemExit(f"fake gh: unsupported command {args}")
    path.write_text(json.dumps(state, indent=2))
    if result is not None:
        print(json.dumps(result) if isinstance(result, (dict, list)) else result)


def install(bin_dir: Path) -> Path:
    """Write a `gh` executable that runs this module with the harness's interpreter."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "gh"
    target.write_text(f"#!{sys.executable}\nfrom cardinal_harness.fake_gh import main\nmain()\n")
    target.chmod(0o755)
    return target


if __name__ == "__main__":
    main()
