"""A stand-in for the `gh` CLI over a real bare Git remote, for offline end-to-end runs.

Modelled on gh's documented commands and JSON fields, not on what Cardinal happens to call
(lessons/fakes-model-the-vendor.md). Only commands Cardinal also runs against real GitHub in the
live suite are accepted; anything else exits nonzero so a new call cannot pass offline unnoticed.
Branch heads are read from the bare remote on every call, as GitHub would report them.

State lives in the JSON file named by CARDINAL_FAKE_GH_STATE:
  repository, bare, issues{number: {...}}, labels[], prs[], next_pr,
  ci{"script": ["pending", "success"], "check": "name", "scripts": [[...], ...], "log": "...",
     "also": ["name"], "push": [{"name": ["failure", "success"]}, ...]},
  check_calls{sha: n}. With "scripts", the Nth distinct head checked follows the Nth script (the last
  repeats), so a repaired head can pass where the first failed. "also" names checks that pass on every
  PR head once the scripted one is listed. "log" is every failed job's log.
  "push" is the base branch's CI on merge commits: the Nth merge follows the Nth entry (the last
  repeats), where each check lists its conclusion per attempt; every attempt is in progress when first
  listed, and `gh run rerun --failed` starts the next attempt of each failed job.
  other_repos{slug: {issues, labels}}: further repositories that hold only issues and labels

Calls are atomic, as on GitHub: parallel workers' calls are serialised on a lock beside the state.

Like GitHub, `mergeable` is computed from the real refs (`git merge-tree`) and reads UNKNOWN the first
time a head/base pair is asked about; a conflicting PR runs no pull_request CI and cannot be merged.
"""

import fcntl
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
    """As gh prints it: labels and comments are objects. State keeps comments as their bodies."""
    return {**issue, "labels": [{"name": name} for name in issue["labels"]],
            "comments": [{"author": {"login": "fake-user"}, "body": body} for body in issue["comments"]]}


def conflicting(state: dict, pr: dict) -> bool:
    merge = subprocess.run(["git", "--git-dir", state["bare"], "merge-tree", "--write-tree",
                            f"refs/heads/{pr['baseRefName']}", f"refs/heads/{pr['headRefName']}"],
                           text=True, capture_output=True, check=False)
    if merge.returncode not in (0, 1):
        raise SystemExit(f"fake gh: merge-tree failed: {merge.stderr}")
    return merge.returncode == 1


def mergeable(state: dict, pr: dict, sha: str) -> str:
    if pr["state"] != "OPEN":
        return "UNKNOWN"
    key = f"{sha}:{branch_sha(state, pr['baseRefName'])}"
    seen = state.setdefault("mergeable_seen", [])
    if key not in seen:  # GitHub computes it in the background after a push to either side
        seen.append(key)
        return "UNKNOWN"
    return "CONFLICTING" if conflicting(state, pr) else "MERGEABLE"


def pr_json(state: dict, pr: dict, args: list[str]) -> dict:
    sha = branch_sha(state, pr["headRefName"]) if pr["state"] == "OPEN" else pr["headRefOid"]
    item = {**pr, "headRefOid": sha}
    if "mergeable" in (option(args, "--json") or "").split(","):
        item["mergeable"] = mergeable(state, pr, sha)
    return item


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
    if verb == "create":
        taken = [int(number) for number in state["issues"]] + [pr["number"] for pr in state.get("prs", [])]
        number = max(taken, default=0) + 1
        labels = [args[index + 1] for index, arg in enumerate(args) if arg == "--label"]
        for name in labels:
            if name not in state["labels"]:
                raise SystemExit(f"could not add label: '{name}' not found")
        state["issues"][str(number)] = {"number": number, "title": option(args, "--title"), "body": option(args, "--body"),
                                        "state": "OPEN", "labels": labels, "comments": []}
        return f"https://github.invalid/{state['_slug']}/issues/{number}"
    if verb == "comment":
        state["issues"][args[1]]["comments"].append(option(args, "--body"))
        return None
    raise SystemExit(f"fake gh: unsupported issue command {args}")


def pulls(state: dict, args: list[str]):
    verb = args[0]
    if verb == "list":
        head = option(args, "--head")
        found = [pr_json(state, pr, args) for pr in state["prs"]
                 if pr["state"] == "OPEN" and (head is None or pr["headRefName"] == head)]
        return [fields(item, args) for item in found]
    if verb == "view":
        return fields(pr_json(state, next(pr for pr in state["prs"] if str(pr["number"]) == args[1]), args), args)
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
        if conflicting(state, pr):
            raise SystemExit(f"Pull request #{pr['number']} is not mergeable: the merge commit cannot be cleanly created.")
        with tempfile.TemporaryDirectory() as work:
            git("clone", "-q", "--branch", pr["baseRefName"], state["bare"], work)
            git("-c", "user.name=Fake GitHub", "-c", "user.email=fake@github.invalid", "merge", "--no-ff", "-q",
                "-m", f"Merge pull request #{pr['number']}", f"origin/{pr['headRefName']}", cwd=work)
            git("push", "-q", "origin", f"HEAD:refs/heads/{pr['baseRefName']}", cwd=work)
            merged = git("rev-parse", "HEAD", cwd=work)
        pr.update(state="MERGED", headRefOid=current, mergeCommit={"oid": merged})
        body = (pr.get("body") or "").lower()
        for number, issue in state["issues"].items():
            if f"closes #{number}" in body:
                issue["state"] = "CLOSED"
        return None
    raise SystemExit(f"fake gh: unsupported pr command {args}")


def api(state: dict, args: list[str]):
    path = args[0].split("?")[0]
    parts = path.split("/")
    if len(parts) == 7 and parts[0] == "repos" and parts[3] == "actions" and parts[4] == "jobs" and parts[6] == "logs":
        if "--allow-escape-sequences" not in args:  # real gh refuses to print a log's escape codes without it
            raise SystemExit("the response contains terminal escape sequences; pass --allow-escape-sequences")
        return state["ci"].get("log", "2026-01-01T00:00:00.0000000Z \x1b[31mFAILED\x1b[0m tests/test_cli.py::test_ci_only")
    if len(parts) == 6 and parts[0] == "repos" and parts[3] == "commits" and parts[5] == "check-runs":
        sha = parts[4]
        merges = [pr["mergeCommit"]["oid"] for pr in state["prs"] if pr.get("mergeCommit")]
        if sha in merges:
            return push_checks(state, sha, merges.index(sha))
        if any(pr["state"] == "OPEN" and branch_sha(state, pr["headRefName"]) == sha and conflicting(state, pr)
               for pr in state["prs"]):
            return {"total_count": 0, "check_runs": []}  # GitHub runs no pull_request CI on a conflicting PR
        calls = state["check_calls"].get(sha, 0)
        state["check_calls"][sha] = calls + 1
        heads = state["ci"].setdefault("heads", [])
        if sha not in heads:
            heads.append(sha)
        scripts = state["ci"].get("scripts") or [state["ci"]["script"]]
        script = scripts[min(heads.index(sha), len(scripts) - 1)]
        outcome = script[min(calls, len(script) - 1)]
        if outcome == "missing":
            return {"total_count": 0, "check_runs": []}
        runs = [{"id": calls + 1, "name": state["ci"]["check"], "head_sha": sha,
                 "status": "in_progress" if outcome == "pending" else "completed",
                 "conclusion": None if outcome == "pending" else outcome}]
        runs += [{"id": calls + 1, "name": name, "head_sha": sha, "status": "completed", "conclusion": "success"}
                 for name in state["ci"].get("also", [])]
        return {"total_count": len(runs), "check_runs": runs}
    raise SystemExit(f"fake gh: unsupported api path {path}")


def push_checks(state: dict, sha: str, index: int) -> dict:
    specs = state["ci"].get("push") or [{state["ci"]["check"]: ["success"]}]
    spec = specs[min(index, len(specs) - 1)]
    pushed = state.setdefault("push_runs", {}).setdefault(sha, {"run_id": 9000 + index, "jobs": {}})
    runs = []
    for position, (name, conclusions) in enumerate(spec.items()):
        job = pushed["jobs"].setdefault(name, {"attempt": 0, "seen": 0})
        running = job["seen"] == 0
        job["seen"] += 1
        job["conclusion"] = None if running else conclusions[min(job["attempt"], len(conclusions) - 1)]
        job_id = pushed["run_id"] * 100 + position * 10 + job["attempt"]
        runs.append({"id": job_id, "name": name, "head_sha": sha, "status": "in_progress" if running else "completed",
                     "conclusion": job["conclusion"],
                     "details_url": f"https://github.invalid/{state['repository']}/actions/runs/{pushed['run_id']}/job/{job_id}"})
    return {"total_count": len(runs), "check_runs": runs}


def rerun(state: dict, args: list[str]) -> None:
    """`gh run rerun <run-id> --failed`: only completed, failed jobs start a new attempt."""
    pushed = next((item for item in state.get("push_runs", {}).values() if str(item["run_id"]) == args[0]), None)
    if pushed is None:
        raise SystemExit(f"could not find any workflow run with ID {args[0]}")
    if "--failed" not in args:
        raise SystemExit("fake gh: only `gh run rerun --failed` is modelled")
    failed = [job for job in pushed["jobs"].values() if job.get("conclusion") not in (None, "success", "skipped", "neutral")]
    if not failed:
        raise SystemExit(f"run {args[0]} has no failed jobs to rerun")
    for job in failed:
        job.update(attempt=job["attempt"] + 1, seen=0, conclusion=None)


def main() -> None:
    path = Path(os.environ["CARDINAL_FAKE_GH_STATE"])
    with open(f"{path}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        serve(path)


def serve(path: Path) -> None:
    state = json.loads(path.read_text())
    args = sys.argv[1:]
    scope = state
    if "-R" in args:
        index = args.index("-R")
        slug = args[index + 1]
        if slug != state["repository"]:
            if slug not in state.get("other_repos", {}):
                raise SystemExit(f"GraphQL: Could not resolve to a Repository with the name '{slug}'.")
            scope = state["other_repos"][slug]
        args = args[:index] + args[index + 2:]
    elif args[:2] == ["issue", "create"]:
        raise SystemExit("gh issue create needs -R outside a checkout")
    scope["_slug"] = state["repository"] if scope is state else next(
        key for key, value in state["other_repos"].items() if value is scope)
    log = state.setdefault("calls", [])
    log.append(sys.argv[1:])
    if args[:2] == ["auth", "status"]:
        result = "Logged in to github.invalid as fake"
    elif args[:2] == ["repo", "view"]:
        result = {"nameWithOwner": args[2]}
    elif args[:2] == ["label", "create"]:
        if args[2] not in scope["labels"]:
            scope["labels"].append(args[2])
        result = None
    elif args[0] == "issue":
        result = issues(scope, args[1:])
    elif args[0] == "pr":
        result = pulls(state, args[1:])
    elif args[0] == "api":
        result = api(state, args[1:])
    elif args[:2] == ["run", "rerun"]:
        result = rerun(state, args[2:])
    else:
        raise SystemExit(f"fake gh: unsupported command {args}")
    scope.pop("_slug")
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
