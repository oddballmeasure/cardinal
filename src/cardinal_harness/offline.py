"""Offline end-to-end scenarios: the real product against a bare Git remote and a fake `gh`.

`single`: ledger issue 104 through `cardinal run`: two dependent tickets, CI pending then success,
          a merge, and a deploy whose health becomes ready only on the second poll.
`daemon`: four issues through `cardinal daemon --once`: #12 builds on #11's merge, #13 asks a
          person and is then declined via `cardinal resume`, #14 never passes its tests.
`multi-daemon`: two remotes with the same issue number and distinct labels and worker limits;
          unpinned and pinned daemon drains, and ambiguous run rejection.
`base-sync`: other merges land on main while #11 and #12 are worked; conflicts and a clean move are
          merged in, resolved, verified again and merged.
`post-merge`: checks that fail on merge commits are re-run once; a repeat failure files a follow-up.
`parallel`: three workers: #11, #17 and #18 overlap, #12 waits for #11, planning never overlaps, and
          #17 and #18 both append to the README, so the later merge brings in the earlier one.
`daemon-restart`: graceful daemon SIGTERM/SIGINT mid-run requeues serial and parallel workers.
`worker-killed`: a run killed outright is settled as failed by the next daemon poll, then reruns.
`scout`: `cardinal scout` proposes reviewed, code-checked issues and learns from their outcomes.
Every pass is judged from outside: fake-GitHub state, the bare remote's refs, `status --json`,
and the independent acceptance tests in tests/acceptance.
"""

import itertools
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cardinal_harness import cleanup, fake_gh
from cardinal_harness.product import FIXTURES, ROOT, Product, json_file, product_env, write_config

SLUG = "example/ledger"
CHECK = "ledger-ci"
LABELS = ["cardinal:ready", "cardinal:in-progress", "cardinal:done", "cardinal:error", "cardinal:needs-human"]
ISSUES = {
    104: ("Filter transactions by date and export CSV", json.loads((FIXTURES / "issue_104.json").read_text())["body"]),
    11: ("Filter transactions by inclusive date",
         "- R1: Add --since YYYY-MM-DD; include transactions on that date and later, preserving input order.\n"
         "- R2: Reject impossible --since dates with a nonzero exit and an error that includes the supplied date."),
    12: ("Export transactions as CSV",
         "Builds on #11.\n\n- R1: Add --format csv with columns id,date,merchant,amount; quote commas, keep Unicode.\n"
         "- R2: CSV with no matching transactions still prints its header.\n- R3: --since and --format csv work together."),
    13: ("Purge transactions older than one year", "- R1: Permanently delete every transaction dated over a year ago."),
    14: ("Add a total flag", "- R1: --total prints the sum of the selected amounts."),
    15: ("Archive transactions older than one year", "- R1: Move transactions dated over a year ago to an archive."),
    16: ("Summarise spending by merchant", "- R1: --by-merchant prints each merchant's total."),
    17: ("Note the merchant filter in the README", "- R1: The README mentions --merchant."),
    18: ("Note the input flag in the README", "- R1: The README mentions --input."),
    19: ("Note usage in the README", "- R1: README documents `python -m ledger --help` for usage information."),
    20: ("Change ledger query implementation", "- R1: The query module describes transaction filtering."),
    21: ("Note the absent flag in the README", "- R1: The README mentions the absent flag."),
    22: ("Change ledger TypeScript client", "- R1: The TypeScript client describes transaction filtering."),
    23: ("Change ledger requirements", "- R1: The requirements file describes transaction filtering."),
}
ACCEPTANCE = {104: ["test_since_is_inclusive", "test_invalid_since_is_rejected", "test_csv_preserves_quoting_and_unicode",
                    "test_empty_csv_keeps_header", "test_since_and_csv_combine"]}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()


def world(temp: Path, ready: list[int], ci: list[str] | None = None,
          deploy_case: str | None = None) -> tuple[Path, Path, dict]:
    """A bare remote seeded with the ledger fixture, and fake GitHub state with the given ready issues."""
    seed, bare = temp / "seed", temp / "remote.git"
    shutil.copytree(FIXTURES / "repo", seed)
    if deploy_case:
        shutil.rmtree(seed / "deploy")
        shutil.copytree(FIXTURES / "deploy_cases" / deploy_case, seed, dirs_exist_ok=True)
    git(seed, "init", "-q", "-b", "main")
    git(seed, "add", ".")
    git(seed, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "ledger baseline")
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    state = {
        "repository": SLUG, "bare": str(bare), "labels": list(LABELS), "prs": [], "next_pr": 1,
        "ci": {"script": ci or ["missing", "pending", "success"], "check": CHECK}, "check_calls": {},
        "issues": {str(number): {"number": number, "title": title, "body": body, "state": "OPEN",
                                 "labels": ["cardinal:ready"] if number in ready else [], "comments": []}
                   for number, (title, body) in ISSUES.items()},
    }
    state_path = temp / "github.json"
    json_file(state_path, state)
    return bare, state_path, state


def configure(temp: Path, bare: Path, state_path: Path, artifact: Path, required_checks: list[str] | None = None,
              env: dict[str, str] | None = None, max_parallel_runs: int | None = None,
              limits: dict[str, int] | None = None) -> Product:
    home = temp / "home"
    write_config(home, models={role: "replay:scripted" for role in
                               ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer", "monitor")},
                 slug=SLUG, remote_url=str(bare), test_command=[sys.executable, "-m", "pytest", "-q", "tests"],
                 required_checks=required_checks or [CHECK], paths_off_limits=[".github/"], deploy="local",
                 limits={"coder_attempts": 2, "ci_poll_seconds": 1, "ci_timeout_seconds": 120, **(limits or {})},
                 max_parallel_runs=max_parallel_runs)
    fake_bin = fake_gh.install(temp / "bin")
    env = product_env({
        "PATH": f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}",
        "CARDINAL_FAKE_GH_STATE": str(state_path),
        "CARDINAL_MODEL_PROVIDER": "cardinal_harness.replay_provider:provider",
        "CARDINAL_DEPLOY_HOST": "cardinal_harness.mock_host:factory",
        "CARDINAL_MOCK_HOST_FIXTURE": str(FIXTURES / "deploy_hosts" / "default.json"),
        **(env or {}),
    })
    return Product(home, env, artifact)


def acceptance(bare: Path, temp: Path, number: int) -> dict:
    checkout = temp / f"accept-{number}"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    selector = " or ".join(ACCEPTANCE[number])
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests" / "acceptance" / "test_transactions.py"),
                             "-k", selector, "-p", "no:cacheprovider"], cwd=ROOT, text=True, capture_output=True,
                            env={**os.environ, "CARDINAL_TARGET_REPO": str(checkout)}, check=False, timeout=600)
    return {"exit_code": result.returncode, "stdout": result.stdout[-4000:], "selector": selector}


def expect(checks: dict, name: str, ok: bool, detail: object = None) -> None:
    checks[name] = {"ok": bool(ok), "detail": detail}


def single(temp: Path, artifact: Path) -> dict:
    bare, state_path, _ = world(temp, ready=[104])
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}
    base = git(temp / "seed", "rev-parse", "HEAD")
    expect(checks, "acceptance fails at baseline", acceptance(bare, temp / "baseline", 104)["exit_code"] != 0)
    code, result = product("run", "104")
    expect(checks, "cardinal run exits 0", code == 0, result)
    state = json.loads(state_path.read_text())
    runs = product.status(104)
    run = runs[-1] if runs else {}
    main = git(bare, "rev-parse", "refs/heads/main")
    merged = [pr for pr in state["prs"] if pr["state"] == "MERGED"]
    expect(checks, "one PR opened and merged", len(state["prs"]) == 1 and len(merged) == 1, state["prs"])
    expect(checks, "main advanced to the recorded merge", main != base and run.get("merge_sha") == main,
           {"main": main, "run": run.get("merge_sha")})
    expect(checks, "merged head is the verified head", bool(merged) and merged[0]["headRefOid"] == run.get("head_sha"))
    expect(checks, "CI was observed pending before success",
           any(event["kind"] == "events" and [item.get("status") for item in event["data"] if item.get("action") == "ci"][:1] == ["pending"]
               for event in run.get("events", [])))
    expect(checks, "two tickets committed in dependency order",
           [event["data"]["ticket"] for event in run.get("events", []) if event["kind"] == "committed"]
           == ["ISSUE-104-DATE", "ISSUE-104-CSV"])
    expect(checks, "deployed revision is the merge", run.get("deployed_sha") == main, run.get("deployed_sha"))
    expect(checks, "issue closed and labelled done", state["issues"]["104"]["state"] == "CLOSED"
           and state["issues"]["104"]["labels"] == ["cardinal:done"], state["issues"]["104"])
    accepted = acceptance(bare, temp / "final", 104)
    expect(checks, "independent acceptance passes on merged main", accepted["exit_code"] == 0, accepted)
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "status": runs, "invocations": product.invocations}


def daemon(temp: Path, artifact: Path) -> dict:
    bare, state_path, _ = world(temp, ready=[11, 12, 13, 14, 15])
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}
    code, results = product("daemon", "--once")
    expect(checks, "daemon exits nonzero because #14 failed", code == 1, results)
    by_issue = {item["issue"]: item for item in results} if isinstance(results, list) else {}
    expect(checks, "issues were taken in number order", list(by_issue) == [11, 12, 13, 14, 15], list(by_issue))
    expect(checks, "#11 done", by_issue.get(11, {}).get("status") == "done")
    expect(checks, "#12 done on top of #11's merge", by_issue.get(12, {}).get("status") == "done")
    expect(checks, "#13 waits for a person", by_issue.get(13, {}).get("status") == "awaiting_human")
    expect(checks, "#15 waits for a person", by_issue.get(15, {}).get("status") == "awaiting_human")
    expect(checks, "#14 failed on its tests", (by_issue.get(14, {}).get("failure") or {}).get("kind") == "tests_failed",
           by_issue.get(14))
    state = json.loads(state_path.read_text())
    labels = {number: state["issues"][str(number)]["labels"] for number in (11, 12, 13, 14)}
    expect(checks, "labels settled on one state axis", labels == {11: ["cardinal:done"], 12: ["cardinal:done"],
                                                                  13: ["cardinal:needs-human"], 14: ["cardinal:error"]}, labels)
    expect(checks, "#13 got its question as a comment", any("decision" in comment for comment in state["issues"]["13"]["comments"]))
    run12 = product.status(12)[-1]
    merge11 = product.status(11)[-1]["merge_sha"]
    expect(checks, "#12 branch contains #11's merge",
           subprocess.run(["git", "--git-dir", str(bare), "merge-base", "--is-ancestor", merge11, run12["head_sha"]]).returncode == 0)
    code, again = product("daemon", "--once")
    expect(checks, "a second drain claims nothing", code == 0 and again == [], again)
    state = json.loads(state_path.read_text())  # a person puts failed #14 back in the queue
    state["issues"]["14"]["labels"] = ["cardinal:ready"]
    json_file(state_path, state)
    code, retried = product("daemon", "--once")
    retry = (retried or [{}])[0] if isinstance(retried, list) else {}
    expect(checks, "a requeued failure is retried as a fresh run over its old branch",
           retry.get("issue") == 14 and (retry.get("failure") or {}).get("kind") == "tests_failed"
           and len(product.status(14)) == 2, retried)
    code, resumed = product("resume", "13", "--reject", "--note", "Do not delete data")
    expect(checks, "resume --reject ends #13 rejected", code == 4 and isinstance(resumed, dict)
           and resumed.get("status") == "rejected", resumed)
    orchestrations = [call for call in product.status(13)[-1]["agent_calls"] if call["stage"] == "orchestrator"]
    expect(checks, "declining did not repeat the orchestrator call", len(orchestrations) == 1, len(orchestrations))
    code, approved = product("resume", "15", "--approve", "--note", "Keep every record; archive nothing yet")
    expect(checks, "resume --approve carries #15 through to done", code == 0 and isinstance(approved, dict)
           and approved.get("status") == "done", approved)
    run15 = product.status(15)[-1]
    answers = [event["data"] for event in run15["events"] if event["kind"] == "answer"]
    replanned = [event["data"]["reason"] for event in run15["events"] if event["kind"] == "decision"]
    expect(checks, "the approval note reached the second intake",
           answers and answers[0].get("note", "").startswith("Keep every record")
           and any("Keep every record" in reason for reason in replanned), {"answers": answers, "decisions": replanned})
    final = json.loads(state_path.read_text())["issues"]["15"]
    expect(checks, "#15 closed and labelled done", final["state"] == "CLOSED" and final["labels"] == ["cardinal:done"], final)
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def repos_check(temp: Path, artifact: Path) -> dict:
    """Check two remotes through the CLI, including a missing branch in only one."""
    second = "example/other-ledger"
    bare, state_path, state = world(temp, ready=[])
    other_dir = temp / "other"
    other_dir.mkdir()
    other_bare, _, other = world(other_dir, ready=[])
    other["repository"] = second
    state["other_repos"] = {second: other}
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact)
    config = product.home / "cardinal.toml"
    with config.open("a") as output:
        output.write(f'\n[[repos]]\nslug = "{second}"\nremote_url = {json.dumps(str(other_bare))}\n'
                     'base_branch = "main"\ntest_command = ["git", "--version"]\n'
                     'required_checks = []\npaths_off_limits = []\npaths_hidden = []\n')
    checks: dict = {}
    code, passed = product("repos", "check")
    expect(checks, "unpinned check reports both healthy repositories",
           code == 0 and isinstance(passed, list) and [item.get("repo") for item in passed] == [SLUG, second]
           and all(item.get("ok") and item.get("checks") and
                   all(value.startswith("ok") for value in item["checks"].values()) for item in passed), passed)

    # Break only the second remote's base-branch probe; the first still passes.
    text = config.read_text()
    before, separator, after = text.rpartition('base_branch = "main"')
    assert separator
    config.write_text(before + 'base_branch = "missing"' + after)
    code, partial = product("repos", "check")
    expect(checks, "one failed probe cannot mask the other repository or its successful checks",
           code == 1 and isinstance(partial, list) and len(partial) == 2
           and [item.get("repo") for item in partial] == [SLUG, second]
           and partial[0].get("ok") is True and partial[1].get("ok") is False
           and partial[1].get("checks", {}).get("base branch", "").startswith("FAILED")
           and partial[1]["checks"].get("repository", "").startswith("ok")
           and partial[1]["checks"].get("test command", "").startswith("ok"), partial)

    calls_before = len(json.loads(state_path.read_text())["calls"])
    code, pinned = product("repos", "check", "--repo", SLUG)
    new_calls = json.loads(state_path.read_text())["calls"][calls_before:]
    expect(checks, "pinned check probes only the selected repository",
           code == 0 and isinstance(pinned, dict) and pinned.get("repo") == SLUG and pinned.get("ok") is True
           and all(second not in call for call in new_calls), {"result": pinned, "calls": new_calls})
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def multi_daemon(temp: Path, artifact: Path) -> dict:
    """Two independent remotes share issue #19, but not labels, clones, claims or worker limits."""
    second = "example/other-ledger"
    bare, state_path, state = world(temp, ready=[19], ci=["success"])
    other_dir = temp / "other"
    other_dir.mkdir()
    other_bare, _, other = world(other_dir, ready=[19], ci=["success"])
    other["repository"] = second
    other["issues"]["19"]["labels"] = ["other:ready"]
    other["labels"] += ["other:ready", "other:working"]
    state["other_repos"] = {second: other}
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact, required_checks=[], max_parallel_runs=1)
    with (product.home / "cardinal.toml").open("a") as config:
        config.write(f'\n[[repos]]\nslug = "{second}"\nremote_url = {json.dumps(str(other_bare))}\n'
                     f'base_branch = "main"\ntest_command = {json.dumps([sys.executable, "-m", "pytest", "-q", "tests"])}\n'
                     'required_checks = []\npaths_off_limits = [".github/"]\npaths_hidden = []\n'
                     'max_parallel_runs = 2\n\n[repos.labels]\nready = "other:ready"\nworking = "other:working"\n')
    checks: dict = {}
    code, ambiguous = product("run", "19")
    error_log = Path(product.invocations[-1]["log"]).read_text()
    expect(checks, "run rejects ambiguous issue numbers with --repo guidance",
           code == 2 and ambiguous is None and
           "Several repositories are configured; pass --repo owner/name" in error_log, error_log[-1000:])
    code, pinned = product("daemon", "--once", "--repo", SLUG)
    state = json.loads(state_path.read_text())
    expect(checks, "pinned daemon completes only the selected repo",
           code == 0 and isinstance(pinned, list) and len(pinned) == 1 and pinned[0]["status"] == "done"
           and state["issues"]["19"]["labels"] == ["cardinal:done"]
           and state["other_repos"][second]["issues"]["19"]["labels"] == ["other:ready"], pinned)
    code, unpinned = product("daemon", "--once")
    state = json.loads(state_path.read_text())
    other_issue = state["other_repos"][second]["issues"]["19"]
    expect(checks, "unpinned daemon completes the other repo with its own ready label and worker limit",
           code == 0 and isinstance(unpinned, list) and len(unpinned) == 1 and unpinned[0]["status"] == "done"
           and other_issue["state"] == "CLOSED" and other_issue["labels"] == ["cardinal:done"], unpinned)
    runs = product.status(19)
    expect(checks, "same issue number got a separate run and merge in each repository",
           {run["repo"] for run in runs} == {SLUG, second} and len(runs) == 2 and len(state["prs"]) == 1
           and len(state["other_repos"][second]["prs"]) == 1
           and all(pr["state"] == "MERGED" for pr in state["prs"] + state["other_repos"][second]["prs"]),
           {"prs": state["prs"], "other_prs": state["other_repos"][second]["prs"]})
    # Reset both remotes to a fresh ready issue to prove that one unpinned invocation drains both.
    for slug, scope, label in ((SLUG, state, "cardinal:ready"), (second, state["other_repos"][second], "other:ready")):
        scope["issues"]["17"]["labels"] = [label]
    json_file(state_path, state)
    code, both = product("daemon", "--once")
    state = json.loads(state_path.read_text())
    expect(checks, "one unpinned daemon invocation settles both ready issues",
           code == 0 and isinstance(both, list) and len(both) == 2
           and all(result["status"] == "done" for result in both)
           and all(scope["issues"]["17"]["labels"] == ["cardinal:done"] and scope["issues"]["17"]["state"] == "CLOSED"
                   for scope in (state, state["other_repos"][second])), both)
    records = [json.loads(line) for path in (product.home / "logs").glob("*.jsonl")
               for line in path.read_text().splitlines()]
    worker_issues = [record["data"]["issue"] for record in records if record.get("event") == "worker_started"]
    locks = product.home / "clones"
    expect(checks, "only the parallel-configured repo starts workers and each clone has its own intake lock",
           sorted(worker_issues) == [17, 19] and all((locks / f"{slug.replace('/', '--')}.intake.lock").exists()
                                                       for slug in (SLUG, second)), worker_issues)
    product.keep_store()
    json_file(artifact / "github_state.json", state)
    return {"checks": checks, "invocations": product.invocations}


def ci_failure(temp: Path, artifact: Path) -> dict:
    bare, state_path, state = world(temp, ready=[104], ci=["pending", "failure"])
    state["ci"]["also"] = ["ledger-lint"]
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact, required_checks=[CHECK, "ledger-lint"])
    base = git(bare, "rev-parse", "refs/heads/main")
    checks: dict = {}
    code, result = product("run", "104")
    expect(checks, "cardinal run exits 1", code == 1, result)
    failure = (result.get("failure") or {}) if isinstance(result, dict) else {}
    expect(checks, "failure is ci_failed", failure.get("kind") == "ci_failed", result)
    expect(checks, "the failure names only the check that failed",
           f"['{CHECK}'] failed" in failure.get("detail", "") and "ledger-lint" not in failure.get("detail", ""), failure)
    state = json.loads(state_path.read_text())
    expect(checks, "the PR stays open and unmerged", [pr["state"] for pr in state["prs"]] == ["OPEN"], state["prs"])
    expect(checks, "main did not move", git(bare, "rev-parse", "refs/heads/main") == base)
    issue = state["issues"]["104"]
    expect(checks, "issue labelled error with the reason", issue["labels"] == ["cardinal:error"]
           and any("ci_failed" in comment for comment in issue["comments"]), issue)
    run = product.status(104)[-1]
    expect(checks, "nothing was deployed", run.get("deployed_sha") is None and run.get("merge_sha") is None, run.get("deployed_sha"))
    repairs = [event["data"] for event in run["events"] if event["kind"] == "ci_repair"]
    coders = [call for call in run["agent_calls"] if call["stage"] == "coder" and call["ticket_id"] == "CI-REPAIR-1"]
    expect(checks, "one CI repair round got the failed job's log, then the run gave up",
           len(repairs) == 1 and "FAILED tests/test_cli.py::test_ci_only" in repairs[0]["log"]
           and "\x1b" not in repairs[0]["log"] and len(coders) == 1 and len(state["ci"]["heads"]) == 2,
           {"repairs": repairs, "heads": state["ci"].get("heads")})
    product.keep_store()
    json_file(artifact / "github_state.json", state)
    return {"checks": checks, "invocations": product.invocations}


def ci_repair(temp: Path, artifact: Path) -> dict:
    """The first pushed head fails CI; the coder gets the log, the repaired head passes and merges."""
    bare, state_path, _ = world(temp, ready=[104], ci=["pending", "failure"])
    state = json.loads(state_path.read_text())
    state["ci"]["scripts"] = [["pending", "failure"], ["pending", "success"]]
    state["ci"]["log"] = "2026-01-01T00:00:00.0000000Z FAILED tests/test_cli.py::test_runner_timezone - assert 2 == 3"
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}
    code, result = product("run", "104")
    expect(checks, "cardinal run exits 0 after one CI repair", code == 0, result)
    state = json.loads(state_path.read_text())
    run = product.status(104)[-1]
    tickets = json.loads((product.home / "runs" / run["run_id"] / "context" / "ticket.json").read_text())
    expect(checks, "the repair ticket carries the CI log, without timestamps",
           tickets["id"] == "CI-REPAIR-1" and "FAILED tests/test_cli.py::test_runner_timezone" in tickets["task"]
           and "2026-01-01T00:00:00" not in tickets["task"], tickets.get("task", "")[-600:])
    heads = state["ci"]["heads"]
    pr = state["prs"][0] if state["prs"] else {}
    expect(checks, "the same PR merged the repaired head, which CI passed",
           len(state["prs"]) == 1 and pr.get("state") == "MERGED" and len(heads) == 2
           and pr.get("headRefOid") == heads[1] == run["head_sha"], {"heads": heads, "pr": pr})
    verdicts = [event for event in run["events"] if event["kind"] == "verdict"]
    expect(checks, "the verifier checked the repaired head again", len(verdicts) == 2, len(verdicts))
    after = acceptance(bare, temp / "after", 104)
    expect(checks, "acceptance passes on the new main", after["exit_code"] == 0, after)
    product.keep_store()
    json_file(artifact / "github_state.json", state)
    return {"checks": checks, "invocations": product.invocations}


def appended_test(name: str, merchant: str, ids: list[str]) -> str:
    return (f"\n\ndef {name}() -> None:\n"
            "    result = subprocess.run([sys.executable, '-m', 'ledger', '--input', 'data/transactions.json',\n"
            f"                             '--merchant', {merchant!r}], text=True, capture_output=True, check=False)\n"
            "    assert result.returncode == 0, result.stderr\n"
            f"    assert [row['id'] for row in json.loads(result.stdout)] == {ids!r}\n")


def parents(bare: Path, sha: str) -> list[str]:
    return git(bare, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def base_sync(temp: Path, artifact: Path) -> dict:
    """Other PRs merge into main while the daemon works #11 then #12, like koizler #15 (cardinal#3).
    #11: a test appended to the file its ticket rewrites lands while it is coded, so the pre-publish
    sync conflicts. #12: a new test file lands while it is coded (a clean move, tested and verified
    again), then another conflicting test lands after its PR opens, so waiting for CI sees the PR
    conflicting and sends it back to sync. Both use the default two sync rounds and merge."""
    bare, state_path, _ = world(temp, ready=[11, 12])
    landings = temp / "landings.json"
    json_file(landings, [
        {"issue": 11, "stage": "coder", "message": "Cover an unknown merchant",
         "append": {"tests/test_cli.py": appended_test("test_unknown_merchant_selects_nothing", "Nobody", [])}},
        {"issue": 12, "stage": "coder", "message": "Cover the books merchant in its own file",
         "write": {"tests/test_books.py": "import json\nimport subprocess\nimport sys" + appended_test(
             "test_books_merchant", "Books", ["t-003"])}},
        {"issue": 12, "stage": "pr_manager", "message": "Cover merchant names with a comma",
         "append": {"tests/test_cli.py": appended_test("test_merchant_with_comma", "Office, East", ["t-001"])}},
    ])
    product = configure(temp, bare, state_path, artifact, env={"CARDINAL_REPLAY_LANDINGS": str(landings)})
    checks: dict = {}
    code, results = product("daemon", "--once")
    by_issue = {item["issue"]: item for item in results} if isinstance(results, list) else {}
    expect(checks, "both issues done", code == 0 and [by_issue.get(n, {}).get("status") for n in (11, 12)] == ["done", "done"],
           results)
    landed = [item.get("landed") for item in json.loads(landings.read_text())]
    expect(checks, "all three other merges landed", all(landed), landed)
    state = json.loads(state_path.read_text())
    run11, run12 = product.status(11)[-1], product.status(12)[-1]
    syncs = {number: [event["data"] for event in run["events"] if event["stage"] == "sync" and event["kind"] == "merged"]
             for number, run in ((11, run11), (12, run12))}
    expect(checks, "#11 merged main in once, with a conflict in the rewritten test file",
           [(item["base_sha"], item["conflicts"]) for item in syncs[11]] == [(landed[0], ["tests/test_cli.py"])], syncs[11])
    expect(checks, "#12 merged a clean move, then a conflict GitHub reported while waiting for CI",
           [(item["base_sha"], item["conflicts"]) for item in syncs[12]] == [(landed[1], []), (landed[2], ["tests/test_cli.py"])],
           syncs[12])
    ci12 = [item.get("status") for event in run12["events"] if event["kind"] == "events"
            for item in event["data"] if item.get("action") == "ci"]
    expect(checks, "#12's first wait stopped on the conflict, not a timeout",
           "conflict" in ci12 and "timeout" not in ci12 and ci12[-1] == "success", ci12)
    expect(checks, "#12 kept its PR and reported the conflict", any(event["kind"] == "conflict" for event in run12["events"])
           and [pr["state"] for pr in state["prs"]] == ["MERGED", "MERGED"], state["prs"])
    tickets = {number: [event["data"]["ticket"] for event in run["events"] if event["kind"] == "committed"]
               for number, run in ((11, run11), (12, run12))}
    expect(checks, "conflicts were resolved through SYNC tickets", tickets == {11: ["DATE", "SYNC-1"], 12: ["CSV", "SYNC-2"]},
           tickets)
    verdicts = {number: len([event for event in run["events"] if event["kind"] == "verdict"]) for number, run in ((11, run11), (12, run12))}
    expect(checks, "every new head was verified again", verdicts == {11: 2, 12: 3}, verdicts)
    merged = {pr["number"]: pr["headRefOid"] for pr in state["prs"]}
    expect(checks, "each merged head is its run's verified head and contains what landed",
           merged == {1: run11["head_sha"], 2: run12["head_sha"]}
           and landed[0] in parents(bare, run11["head_sha"]) and landed[2] in parents(bare, run12["head_sha"]), merged)
    expect(checks, "CI was awaited only on the two merged heads", state["ci"].get("heads") == [merged.get(1), merged.get(2)],
           state["ci"].get("heads"))
    checkout = temp / "final"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"], cwd=checkout,
                           text=True, capture_output=True, check=False, timeout=600)
    kept = {number: git(bare, "show", f"{merged.get(number)}:tests/test_cli.py") if merged.get(number) else ""
            for number in (1, 2)}
    expect(checks, "each resolution kept both sides' tests, and main passes them",
           suite.returncode == 0 and (checkout / "tests" / "test_books.py").exists()
           and all(f"def {name}(" in kept[1] for name in ("test_unknown_merchant_selects_nothing",
                                                          "test_since_includes_boundary_and_later"))
           and all(f"def {name}(" in kept[2] for name in ("test_merchant_with_comma", "test_csv_filter_preserves_unicode")),
           {"stdout": suite.stdout[-2000:], "kept": kept})
    expect(checks, "independent acceptance for these features passes on main", acceptance(bare, temp / "acc", 104)["exit_code"] == 0)
    product.keep_store()
    json_file(artifact / "github_state.json", state)
    return {"checks": checks, "invocations": product.invocations}


def post_merge(temp: Path, artifact: Path) -> dict:
    """Checks on merge commits finish after Cardinal merged. #11's merge has a flaky check that passes
    when re-run: nothing is filed. #12's merge has one that fails twice: one follow-up issue, labelled
    ready, which the daemon then fixes; that fix's merge fails the same way and goes to a person.
    Every `daemon --once` is a fresh process, so judgements must survive restarts."""
    bare, state_path, state = world(temp, ready=[11, 12])
    state["ci"]["push"] = [{CHECK: ["success"], "browser-e2e": ["failure", "success"]},
                           {CHECK: ["success"], "browser-e2e": ["failure", "failure"]}]
    state["ci"]["log"] = "2026-01-01T00:00:00.0000000Z FAILED tests/e2e/test_browser.py::test_flow - Timeout 30000ms"
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}
    code, first = product("daemon", "--once")
    expect(checks, "#11 and #12 merged", code == 0 and [item.get("status") for item in first or []] == ["done", "done"], first)
    polls = []
    for _ in range(10):
        code, results = product("daemon", "--once")
        state = json.loads(state_path.read_text())
        polls.append({"exit": code, "issues": sorted(state["issues"], key=int), "results": results})
    state = json.loads(state_path.read_text())
    filed = {number: issue for number, issue in state["issues"].items() if issue["title"].startswith("CI failed on main")}
    merges = [pr["mergeCommit"]["oid"] for pr in state["prs"] if pr.get("mergeCommit")]
    reruns = [call for call in state["calls"] if call[:2] == ["run", "rerun"]]
    expect(checks, "three merges were judged", len(merges) == 3, state["prs"])
    expect(checks, "each failed merge was re-run once",
           sorted(call[2] for call in reruns) == ["9000", "9001", "9002"] and all("--failed" in call for call in reruns), reruns)
    first_filed = next((issue for issue in filed.values() if "after #2:" in issue["title"]), {})
    created = [call for call in state["calls"] if call[:2] == ["issue", "create"]]
    expect(checks, "the flaky check filed nothing; the repeated failure filed one issue, created ready",
           not any("after #1:" in issue["title"] for issue in filed.values())
           and first_filed.get("title") == "CI failed on main after #2: browser-e2e"
           and any(first_filed["title"] in call and "--label" in call and call[call.index("--label") + 1] == "cardinal:ready"
                   for call in created),
           {"filed": filed, "created": created})
    body = first_filed.get("body") or ""
    expect(checks, "the follow-up names the checks, merge, PR, issue and log",
           all(part in body for part in ("`browser-e2e`", merges[1] if len(merges) > 1 else "?", "#2", "#12",
                                         "FAILED tests/e2e/test_browser.py::test_flow"))
           and "2026-01-01T00:00:00" not in body, body[:1500])
    looped = next((issue for issue in filed.values() if issue is not first_filed), {})
    expect(checks, "the follow-up was fixed and merged, and its own failed merge went to a person",
           first_filed.get("state") == "CLOSED" and len(filed) == 2
           and looped.get("labels") == ["cardinal:needs-human"] and "needs a person" in (looped.get("body") or ""), filed)
    expect(checks, "later polls file nothing more and never fail", polls[-1]["issues"] == polls[-3]["issues"]
           and all(item["exit"] == 0 for item in polls), polls)
    product.keep_store()
    json_file(artifact / "github_state.json", state)
    return {"checks": checks, "invocations": product.invocations}


def lock_spans(home: Path, name: str) -> list[tuple[str, str, int]]:
    """(acquired, released, pid) for each hold of one product lock, from the product's own log files."""
    spans, open_at = [], {}
    records = [json.loads(line) for path in sorted((home / "logs").glob("*.jsonl")) for line in path.read_text().splitlines()]
    for record in sorted(records, key=lambda item: item["at"]):
        if (record.get("data") or {}).get("lock") != name:
            continue
        pid = record["source"]["pid"]
        if record["event"] == "lock_acquired":
            open_at[pid] = record["at"]
        elif record["event"] == "lock_released" and pid in open_at:
            spans.append((open_at.pop(pid), record["at"], pid))
    return spans


def parallel(temp: Path, artifact: Path) -> dict:
    """`daemon --once` with three workers and four ready issues. #12 builds on #11, so it waits for
    #11's merge; #11, #17 and #18 run at once. #17 and #18 each append a line to the README, so
    whichever merges second must bring in the other's line, through a sync before publishing or a
    conflict GitHub reports while it waits for CI. Every merge moves main under the other runs."""
    bare, state_path, _ = world(temp, ready=[11, 12, 17, 18])
    product = configure(temp, bare, state_path, artifact, max_parallel_runs=3, limits={"base_sync_rounds": 4})
    checks: dict = {}
    code, results = product("daemon", "--once")
    by_issue = {item["issue"]: item for item in results} if isinstance(results, list) else {}
    expect(checks, "all four issues done", code == 0 and {n: by_issue.get(n, {}).get("status") for n in (11, 12, 17, 18)}
           == {11: "done", 12: "done", 17: "done", 18: "done"}, results)
    runs = {number: product.status(number)[-1] for number in (11, 12, 17, 18)}
    windows = {number: (run["started_at"], run["ended_at"] or "") for number, run in runs.items()}
    overlapping = [(a, b) for a in (11, 17, 18) for b in (11, 17, 18)
                   if a < b and windows[a][0] < windows[b][1] and windows[b][0] < windows[a][1]]
    expect(checks, "#11, #17 and #18 were worked at the same time", overlapping, windows)
    expect(checks, "#12 started only after #11 settled, on top of #11's merge",
           windows[12][0] >= windows[11][1] and subprocess.run(
               ["git", "--git-dir", str(bare), "merge-base", "--is-ancestor", runs[11]["merge_sha"] or "", runs[12]["head_sha"] or ""],
               capture_output=True, check=False).returncode == 0, windows)
    spans = sorted(lock_spans(product.home, "intake"))
    expect(checks, "planning held the intake lock one run at a time",
           len({pid for _, _, pid in spans}) == 4 and all(later[0] >= earlier[1] for earlier, later in itertools.pairwise(spans)),
           spans)
    state = json.loads(state_path.read_text())
    merges = {pr["headRefName"]: (pr.get("mergeCommit") or {}).get("oid") for pr in state["prs"]}
    expect(checks, "each run recorded its own PR's merge commit, not whatever main was",
           all(run["merge_sha"] and merges.get(run["branch"]) == run["merge_sha"] for run in runs.values())
           and len(set(merges.values())) == 4, {"prs": merges, "runs": {n: (r["branch"], r["merge_sha"]) for n, r in runs.items()}})
    synced = {number: [event["data"] for event in product.status(number)[-1].get("events", [])
                       if event["stage"] == "sync" and event["kind"] == "merged"] for number in (11, 17, 18)}
    expect(checks, "runs merged in main as it moved under them", any(synced.values()), synced)
    checkout = temp / "final"
    subprocess.run(["git", "clone", "-q", str(bare), str(checkout)], check=True)
    readme = (checkout / "README.md").read_text()
    suite = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests", "-p", "no:cacheprovider"], cwd=checkout,
                           text=True, capture_output=True, check=False, timeout=600)
    expect(checks, "main keeps both README lines and passes its tests",
           all(f"{ISSUES[n][0]}.\n" in readme for n in (17, 18)) and suite.returncode == 0,
           {"readme": readme[-400:], "suite": suite.stdout[-1500:]})
    code, again = product("daemon", "--once")
    expect(checks, "a second drain claims nothing", code == 0 and again == [], again)
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def daemon_restart(temp: Path, artifact: Path) -> dict:
    """Stop a real daemon mid-run, then drain the same issue without touching its labels.
    Cover both the in-process run and the daemon's CLI worker, under both graceful signals."""
    checks: dict = {}
    for parallel, signum in ((False, signal.SIGTERM), (True, signal.SIGTERM),
                             (False, signal.SIGINT), (True, signal.SIGINT)):
        name = f"{'parallel' if parallel else 'serial'}-{signum.name.lower()}"
        root = temp / name
        root.mkdir()
        bare, state_path, _ = world(root, ready=[104])
        product = configure(root, bare, state_path, artifact / name, max_parallel_runs=2 if parallel else None)
        output = artifact / f"{name}-daemon.log"
        command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home),
                   "daemon", "--interval", "1"]
        with output.open("w") as log_file:
            daemon_process = subprocess.Popen(command, env=product.env, start_new_session=True,
                                              stdout=log_file, stderr=log_file)
            try:
                deadline = time.monotonic() + 120
                observed = []
                while time.monotonic() < deadline and daemon_process.poll() is None:
                    observed = product.status(104)
                    if observed and observed[-1]["status"] == "running" and observed[-1]["branch"]:
                        break
                    time.sleep(0.2)
                mid_run = bool(observed and observed[-1]["status"] == "running" and observed[-1]["branch"])
                expect(checks, f"{name}: daemon reached an in-flight run", mid_run, observed)
                daemon_process.send_signal(signum)
                try:
                    daemon_process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    os.killpg(daemon_process.pid, signal.SIGKILL)
                    daemon_process.wait()
            finally:
                if daemon_process.poll() is None:
                    os.killpg(daemon_process.pid, signal.SIGKILL)
                    daemon_process.wait()
        before = product.status(104)
        issue_before = json.loads(state_path.read_text())["issues"]["104"]
        expect(checks, f"{name}: shutdown requeued and released its claim",
               mid_run and daemon_process.returncode == 128 + signum
               and len(before) == 1 and before[0]["status"] == "interrupted"
               and issue_before["labels"] == ["cardinal:ready"],
               {"exit": daemon_process.returncode, "runs": before, "issue": issue_before, "log": str(output)})
        code, results = product("daemon", "--once", timeout=900)
        state = json.loads(state_path.read_text())
        issue = state["issues"]["104"]
        labels_added = [call[call.index("--add-label") + 1] for call in state.get("calls", [])
                        if call[:3] == ["issue", "edit", "104"] and "--add-label" in call]
        expect(checks, f"{name}: restart finishes without manual relabel or error",
               code == 0 and isinstance(results, list) and [item.get("status") for item in results] == ["done"]
               and len(product.status(104)) == 2 and issue["state"] == "CLOSED"
               and issue["labels"] == ["cardinal:done"] and "cardinal:error" not in labels_added,
               {"results": results, "issue": issue, "labels_added": labels_added})
        product.keep_store()
        json_file(artifact / name / "github_state.json", state)
    return {"checks": checks}


def worker_killed(temp: Path, artifact: Path) -> dict:
    """A run killed outright (SIGKILL, as the OOM killer would) never releases its claim. The next
    daemon poll settles it as failed and labels it; requeued, the issue runs again to done."""
    bare, state_path, _ = world(temp, ready=[17])
    product = configure(temp, bare, state_path, artifact, max_parallel_runs=2)
    checks: dict = {}
    command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home), "run", "17"]
    # Only the victim gets the hold: its scripted coder writes `reached`, then blocks until killed, so the
    # kill lands before any PR exists. The later `daemon --once` runs do not get it and finish normally.
    gate = temp / "kill-gate"
    gate.mkdir()
    victim = subprocess.Popen(command, env={**product.env, "CARDINAL_REPLAY_HOLD_DIR": str(gate)},
                              start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and not (gate / "reached").exists() and victim.poll() is None:
        time.sleep(0.1)
    reached = (gate / "reached").exists()
    os.killpg(victim.pid, signal.SIGKILL)
    victim.wait()
    at_kill = json.loads(state_path.read_text())
    prs_for_17 = [pr for pr in at_kill.get("prs", [])
                  if re.search(r"(?<!\d)17(?!\d)", f"{pr.get('headRefName', '')} {pr.get('body') or ''}")]
    expect(checks, "at the kill no PR exists for issue 17 and it is still OPEN",
           reached and not prs_for_17 and at_kill["issues"]["17"]["state"] == "OPEN",
           {"reached": reached, "prs": prs_for_17, "state": at_kill["issues"]["17"]["state"]})
    killed = product.status(17)
    expect(checks, "the run was killed while running", [run["status"] for run in killed] == ["running"], killed)
    code, results = product("daemon", "--once")
    failure = ((results or [{}])[0].get("failure") or {}) if isinstance(results, list) else {}
    expect(checks, "the next poll settles it as an abandoned crash", code == 1 and len(results) == 1
           and failure.get("stage") == "abandoned" and failure.get("kind") == "crash", results)
    state = json.loads(state_path.read_text())
    issue = state["issues"]["17"]
    expect(checks, "the issue is labelled error and told why", issue["labels"] == ["cardinal:error"]
           and any("abandoned" in comment for comment in issue["comments"]), issue)
    state["issues"]["17"]["labels"] = ["cardinal:ready"]  # a person puts it back in the queue
    json_file(state_path, state)
    code, rerun = product("daemon", "--once")
    expect(checks, "requeued, it runs again to done", code == 0 and isinstance(rerun, list)
           and [item.get("status") for item in rerun] == ["done"] and len(product.status(17)) == 2, rerun)
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def deploy(temp: Path, artifact: Path) -> dict:
    """Deploy layouts and failures after a real merge: a root-level pair deploys; a script that exits
    nonzero, health that never matches, and an ambiguous layout all fail without a deployed revision."""
    checks: dict = {}
    cases = (("root", "healthy"), ("script_failed", "script_failed"), ("wrong_output", "health_timeout"),
             ("ambiguous", None))
    for case, outcome in cases:
        root = temp / case
        root.mkdir()
        bare, state_path, _ = world(root, ready=[104], deploy_case=case)
        product = configure(root, bare, state_path, artifact / case)
        code, result = product("run", "104")
        run = product.status(104)[-1]
        main = git(bare, "rev-parse", "refs/heads/main")
        issue = json.loads(state_path.read_text())["issues"]["104"]
        reported = [event["data"].get("status") for event in run["events"] if event["kind"] == "result" and event["stage"] == "deploy"]
        expect(checks, f"{case}: merged to main", run.get("merge_sha") == main)
        if outcome == "healthy":
            expect(checks, f"{case}: deployed the merge", code == 0 and run.get("deployed_sha") == main, run.get("deployed_sha"))
            expect(checks, f"{case}: issue labelled done", issue["labels"] == ["cardinal:done"], issue["labels"])
            continue
        expect(checks, f"{case}: run fails as a deploy failure", code == 1 and isinstance(result, dict)
               and (result.get("failure") or {}).get("kind") == "deploy", result)
        expect(checks, f"{case}: nothing recorded as deployed", run.get("deployed_sha") is None, run.get("deployed_sha"))
        expect(checks, f"{case}: deployment ended {outcome or 'before running'}",
               reported == ([outcome] if outcome else []), reported)
        expect(checks, f"{case}: issue labelled error", issue["labels"] == ["cardinal:error"], issue["labels"])
    return {"checks": checks}


def cleaner(temp: Path, artifact: Path) -> dict:
    checks: dict = {}
    done = cleanup.local_scenario(temp / "clean", artifact)
    github = done["github"]
    expect(checks, "cleaner reports clean", done["result"].get("clean") is True, done["result"])
    expect(checks, "remote main restored to base", done["remote"]["main"] == done["base"], done["remote"])
    expect(checks, "managed branch deleted", "cardinal/104-fix" not in done["remote"]["branches"], done["remote"])
    expect(checks, "open managed PR closed, merged PR kept", github["pull_requests"] == {"7": {"state": "merged"}, "8": {"state": "closed"}}, github)
    expect(checks, "issue reopened", github["issues"]["104"]["state"] == "open", github["issues"])
    stale = cleanup.local_scenario(temp / "stale", artifact / "stale", expected_head="0" * 40)
    expect(checks, "stale expected head is refused before any change", "refused" in stale["result"]
           and stale["remote"]["main"] == stale["merged"], stale["result"])
    return {"checks": checks}


def documentation(temp: Path, artifact: Path) -> dict:
    """Real CLI runs: README-only merges without tests, but missing documentation and code tests block."""
    bare, state_path, _ = world(temp, ready=[19, 20, 21, 22, 23])
    product = configure(temp, bare, state_path, artifact, limits={"verify_rounds": 1})
    checks: dict = {}
    base = git(bare, "rev-parse", "refs/heads/main")
    code, result = product("run", "19")
    state = json.loads(state_path.read_text())
    run = product.status(19)[-1]
    verdicts = [event["data"] for event in run["events"] if event["kind"] == "verdict"]
    issue = state["issues"]["19"]
    expect(checks, "README-only issue completes through the CLI and closes as done",
           code == 0 and result.get("status") == "done" and run["status"] == "done"
           and issue["state"] == "CLOSED" and issue["labels"] == ["cardinal:done"],
           {"result": result, "issue": issue})
    main = git(bare, "rev-parse", "refs/heads/main")
    merged = [pr for pr in state["prs"] if pr["state"] == "MERGED"]
    expect(checks, "README PR merged the verified head onto main",
           len(state["prs"]) == 1 and len(merged) == 1 and main != base
           and run["merge_sha"] == main and merged[0]["headRefOid"] == run["head_sha"],
           {"main": main, "base": base, "prs": state["prs"], "run": run})
    readme = git(bare, "show", f"{main}:README.md")
    expected_line = "Use `python -m ledger --help` to view usage information."
    expect(checks, "merged README delivers the requested usage documentation",
           expected_line in readme.splitlines(), readme)
    diff = git(bare, "diff", "--name-only", base, main).splitlines()
    expect(checks, "merged documentation diff changes only README.md, not tests", diff == ["README.md"], diff)
    expect(checks, "verifier accepted the README requirement without claiming boundary tests",
           len(verdicts) == 1 and verdicts[0]["head_sha"] == run["head_sha"]
           and verdicts[0]["approved"] and verdicts[0]["requirements"] == {"R1": True}
           and not verdicts[0]["tests_are_e2e"] and not verdicts[0]["test_files_changed"]
           and verdicts[0]["repo_tests_passed"] and not verdicts[0]["findings"], verdicts)

    for number, path in ((20, "ledger/query.py"), (22, "ledger/client.ts"), (23, "requirements.txt")):
        code, result = product("run", str(number))
        run = product.status(number)[-1]
        verdicts = [event["data"] for event in run["events"] if event["kind"] == "verdict"]
        findings = verdicts[-1]["findings"] if verdicts else []
        revision = json.loads((product.home / "runs" / run["run_id"] / "context" / "revision.json").read_text())
        expect(checks, f"{path} without boundary tests gets both test findings",
               code == 1 and (result.get("failure") or {}).get("kind") == "verification"
               and revision["changed_files"] == [path]
               and "The repository tests added for this change do not exercise the application boundary" in findings
               and "No repository test changed with the code" in findings,
               {"result": result, "revision": revision, "findings": findings})

    code, result = product("run", "21")
    run = product.status(21)[-1]
    verdicts = [event["data"] for event in run["events"] if event["kind"] == "verdict"]
    findings = verdicts[-1]["findings"] if verdicts else []
    expect(checks, "unmet README requirement blocks approval without test findings",
           code == 1 and (result.get("failure") or {}).get("kind") == "verification"
           and any("Requirements not shown to be delivered" in finding for finding in findings)
           and not any("repository test" in finding.lower() for finding in findings),
           {"result": result, "findings": findings})
    expect(checks, "rejected branches did not advance main",
           git(bare, "rev-parse", "refs/heads/main") == product.status(19)[-1]["merge_sha"])
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def file_tool_refusal(temp: Path, artifact: Path) -> dict:
    """A coder meets refused paths, reads the errors, and still finishes a real CLI issue run."""
    bare, state_path, _ = world(temp, ready=[19], ci=["success"])
    outside = temp / "outside-secret"
    outside.write_text("PRIVATE OUTSIDE CONTENT")
    seed = temp / "seed"
    (seed / "hidden.txt").write_text("PRIVATE HIDDEN CONTENT")
    (seed / "app" / "[...slug]").mkdir(parents=True)
    (seed / "app" / "[...slug]" / "page.tsx").write_text("export default function Page() {}\n")
    (seed / "outbound").symlink_to(outside)
    git(seed, "add", "-A")
    git(seed, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "sandbox paths")
    git(seed, "push", str(bare), "HEAD:main")
    product = configure(temp, bare, state_path, artifact, required_checks=[],
                        env={"CARDINAL_REPLAY_FILE_REFUSALS": "1"})
    config = product.home / "cardinal.toml"
    config.write_text(config.read_text().replace('paths_hidden = []', 'paths_hidden = ["hidden.txt"]'))
    code, result = product("run", "19")
    state = json.loads(state_path.read_text())
    run = product.status(19)[-1]
    with sqlite3.connect(product.home / "store.db") as db:
        row = db.execute("SELECT transcript FROM agent_calls WHERE run_id = ? AND stage = 'coder' ORDER BY rowid LIMIT 1",
                         (run["run_id"],)).fetchone()
    messages = json.loads(row[0]) if row else []
    # The scripted coder makes exactly one tool call per model turn.
    results = [message["content"] for message in messages if message["type"] == "tool"]
    checks: dict = {}
    expect(checks, "CLI issue finishes done after the first refused call",
           code == 0 and result.get("status") == "done" and state["issues"]["19"]["labels"] == ["cardinal:done"],
           result)
    expect(checks, "first coder tool result records a traversal refusal",
           bool(results) and "Path traversal not allowed" in results[0], results[:2])
    for index, description in enumerate(("tilde", "outside absolute", "outbound symlink read", "traversal write",
                                         "tilde write", "outside write", "outbound symlink write", "hidden read",
                                         "hidden write", "off-limits write", "context write", "skills write"), 1):
        expect(checks, f"{description} was refused", len(results) > index and
               any(word in results[index].lower() for word in ("error", "refus", "limit", "read-only", "outside",
                                                                  "denied", "traversal", "not allowed")),
               results[index] if len(results) > index else results)
    expect(checks, "component name is readable or recoverably refused", len(results) > 13 and
           ("export default" in results[13] or "error" in results[13].lower()), results[13:14])
    expect(checks, "coder recovered to a permitted README read", len(results) > 14 and
           "ledger" in results[14].lower(), results[14:15])
    expect(checks, "no outside file was modified", outside.read_text() == "PRIVATE OUTSIDE CONTENT"
           and not (temp / "escaped").exists() and not (seed / "escaped").exists())
    product.keep_store()
    return {"checks": checks, "invocations": product.invocations}


def setup_recovery(temp: Path, artifact: Path) -> dict:
    """A partly applied working-label edit, then a setup failure, through real daemon CLI polls."""
    checks: dict = {}
    for name, responses in (
        ("transient", [{"error": "GraphQL: Something went wrong while executing your query",
                        "after": True, "partial": True}]),
        ("permanent", [{"error": "HTTP 404: Not Found", "after": True, "partial": True}]),
    ):
        root = temp / name
        root.mkdir()
        bare, state_path, state = world(root, ready=[19], ci=["success"])
        state["gh_failures"] = [{"command": ["issue", "edit"], "responses": responses}]
        json_file(state_path, state)
        product = configure(root, bare, state_path, artifact / name, required_checks=[])
        code, result = product("daemon", "--once")
        state = json.loads(state_path.read_text())
        rows = product.status(19)
        edits = [call for call in state["calls"] if call[:3] == ["issue", "edit", "19"]]
        observed = [entry["labels"] for entry in state.get("edit_labels", []) if entry["issue"] == "19"]
        expect(checks, f"{name}: first edit really left two labels",
               bool(observed) and set(observed[0]) == {"cardinal:ready", "cardinal:in-progress"}, observed)
        if name == "transient":
            expect(checks, "transient: daemon finishes done after retry, without overlapping runs",
                   code == 0 and [item.get("status") for item in result] == ["done"]
                   and len(rows) == 1 and rows[0]["status"] == "done" and rows[0]["ended_at"]
                   and state["issues"]["19"]["labels"] == ["cardinal:done"] and len(edits) == 3,
                   {"result": result, "rows": rows, "edits": edits, "labels": observed})
        else:
            expect(checks, "permanent: failed setup is settled and restores ready without retrying 404",
                   code != 0 and len(rows) == 1 and rows[0]["status"] == "failed"
                   and rows[0]["failure_kind"] == "setup" and rows[0]["ended_at"]
                   and state["issues"]["19"]["labels"] == ["cardinal:ready"] and len(edits) == 2,
                   {"result": result, "rows": rows, "edits": edits, "labels": observed})
            code, result = product("daemon", "--once")
            state = json.loads(state_path.read_text())
            rows = product.status(19)
            expect(checks, "permanent: released claim permits a fresh run, with no unsettled rows",
                   code == 0 and [item.get("status") for item in result] == ["done"]
                   and [row["status"] for row in rows] == ["failed", "done"]
                   and all(row["ended_at"] for row in rows)
                   and state["issues"]["19"]["labels"] == ["cardinal:done"],
                   {"result": result, "rows": rows, "issue": state["issues"]["19"]})
        product.keep_store()
        json_file(artifact / name / "github_state.json", state)
    return {"checks": checks}


def gh_retries(temp: Path, artifact: Path) -> dict:
    """Script gh at the executable boundary; check CLI results and the vendor's call log."""
    bare, state_path, state = world(temp, ready=[19], ci=["success"])
    state["gh_failures"] = [{"command": ["issue", "edit"], "responses": [
        {"error": "GraphQL: Something went wrong while executing your query", "after": True}]}]
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact, required_checks=[])
    checks: dict = {}
    started = time.monotonic()
    code, result = product("run", "19")
    elapsed = time.monotonic() - started
    state = json.loads(state_path.read_text())
    edits = [call for call in state["calls"] if call[:2] == ["issue", "edit"] and call[2] == "19"]
    expect(checks, "ambiguous label edit recovers without duplicating a run",
           code == 0 and result.get("status") == "done" and len(product.status(19)) == 1
           and state["issues"]["19"]["labels"] == ["cardinal:done"] and len(edits) == 3,
           {"result": result, "edits": edits, "issue": state["issues"]["19"]})
    expect(checks, "transient retry is paced", elapsed >= 0.25, elapsed)

    # Auth is a read-only CLI probe; each case runs in a new process with the same scripted gh.
    for name, error in (("not found", "HTTP 404: Not Found"), ("permission", "HTTP 403: permission denied"),
                        ("validation", "GraphQL: Validation failed: bad input")):
        state = json.loads(state_path.read_text())
        before = len(state["calls"])
        state["gh_failures"] = [{"command": ["auth", "status"], "responses": [{"error": error}]}]
        json_file(state_path, state)
        code, result = product("repos", "check", "--repo", SLUG)
        state = json.loads(state_path.read_text())
        calls = state["calls"][before:]
        expect(checks, f"{name} is not retried", code == 1 and result["checks"]["gh auth"].startswith("FAILED")
               and error in result["checks"]["gh auth"]
               and sum(call[:2] == ["auth", "status"] for call in calls) == 1, {"result": result, "calls": calls})

    for name, error in (("gateway", "HTTP 503: Service Unavailable"),
                        ("graphql", "GraphQL: Something went wrong while executing your query")):
        state = json.loads(state_path.read_text())
        before = len(state["calls"])
        state["gh_failures"] = [{"command": ["auth", "status"], "responses": [{"error": error}] * 3}]
        json_file(state_path, state)
        started = time.monotonic()
        code, result = product("repos", "check", "--repo", SLUG)
        elapsed = time.monotonic() - started
        calls = json.loads(state_path.read_text())["calls"][before:]
        expect(checks, f"{name} exhausts bounded retries with a useful error",
               code == 1 and "after 3 attempts" in result["checks"]["gh auth"]
               and error in result["checks"]["gh auth"] and elapsed >= 0.75
               and sum(call[:2] == ["auth", "status"] for call in calls) == 3,
               {"result": result, "calls": calls, "elapsed": elapsed})
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}


def push_retries(temp: Path, artifact: Path) -> dict:
    """Drive publish through the real CLI and bare remote, faulting only the git executable's push response."""
    checks: dict = {}
    real_git = shutil.which("git")
    assert real_git
    for name, error, apply, expected_pushes in (
        ("remote failure", "remote: fatal error in commit_refs\n ! [remote rejected] HEAD -> cardinal/test (failure)", False, 2),
        ("ambiguous success", "error: connection reset by peer", True, 1),
        ("http", "fatal: HTTP 503 Service Unavailable", False, 2),
        ("reset", "fatal: connection reset by peer", False, 2),
        ("non-fast-forward", "! [rejected] HEAD -> cardinal/test (non-fast-forward)", False, 1),
        ("protected", "! [remote rejected] HEAD -> cardinal/test (protected branch hook declined)", False, 1),
        ("permission", "remote: permission denied\n ! [remote rejected] HEAD -> cardinal/test (failure)", False, 1),
        ("remote moved", "remote: fatal error in commit_refs", False, 1),
        ("exhausted", "remote: fatal error in commit_refs", False, 3),
    ):
        root = temp / name.replace(" ", "-")
        root.mkdir()
        bare, state_path, _ = world(root, ready=[19], ci=["success"])
        bin_dir = root / "git-bin"
        bin_dir.mkdir()
        log = root / "pushes.jsonl"
        shim = bin_dir / "git"
        shim.write_text('''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path
args = sys.argv[1:]
if "push" in args and any(arg.startswith("HEAD:refs/heads/cardinal/") for arg in args):
    path = Path(os.environ["CARDINAL_PUSH_LOG"])
    attempts = len(path.read_text().splitlines()) if path.exists() else 0
    with path.open("a") as output:
        output.write(json.dumps(args) + "\\n")
    if attempts < int(os.environ["CARDINAL_PUSH_FAILURES"]):
        if os.environ["CARDINAL_PUSH_APPLY"] == "1":
            result = subprocess.call([os.environ["CARDINAL_REAL_GIT"], *args])
            if result:
                sys.exit(result)
        if os.environ["CARDINAL_PUSH_MOVE"] == "1":
            branch = next(arg.split(":", 1)[1] for arg in args if arg.startswith("HEAD:refs/heads/cardinal/"))
            subprocess.run([os.environ["CARDINAL_REAL_GIT"], "--git-dir", os.environ["CARDINAL_PUSH_REMOTE"],
                            "update-ref", branch, os.environ["CARDINAL_PUSH_BASE"]], check=True)
        print(os.environ["CARDINAL_PUSH_ERROR"], file=sys.stderr)
        sys.exit(1)
sys.exit(subprocess.call([os.environ["CARDINAL_REAL_GIT"], *args]))
''')
        shim.chmod(0o755)
        product = configure(root, bare, state_path, artifact / name.replace(" ", "-"), required_checks=[],
                            env={"PATH": f"{bin_dir}{os.pathsep}{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
                                 "CARDINAL_REAL_GIT": real_git, "CARDINAL_PUSH_LOG": str(log),
                                 "CARDINAL_PUSH_ERROR": error, "CARDINAL_PUSH_APPLY": "1" if apply else "0",
                                 "CARDINAL_PUSH_MOVE": "1" if name == "remote moved" else "0",
                                 "CARDINAL_PUSH_REMOTE": str(bare),
                                 "CARDINAL_PUSH_BASE": git(bare, "rev-parse", "refs/heads/main"),
                                 "CARDINAL_PUSH_FAILURES": "3" if name == "exhausted" else "1"})
        started = time.monotonic()
        code, result = product("run", "19")
        elapsed = time.monotonic() - started
        pushes = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        state = json.loads(state_path.read_text())
        run = product.status(19)[-1]
        issue = state["issues"]["19"]
        if name in ("remote failure", "ambiguous success", "http", "reset"):
            merged = [pr for pr in state["prs"] if pr["state"] == "MERGED"]
            expect(checks, f"{name}: verified head pushed and merged after failed response",
                   code == 0 and result.get("status") == "done" and issue["labels"] == ["cardinal:done"]
                   and len(merged) == 1 and merged[0]["headRefOid"] == run["head_sha"]
                   and git(bare, "rev-parse", f"refs/heads/{run['branch']}") == run["head_sha"]
                   and len(pushes) == expected_pushes, {"result": result, "pushes": pushes, "run": run})
        else:
            expect(checks, f"{name}: push fails without unsafe retries",
                   code == 1 and (result.get("failure") or {}).get("kind") == "git"
                   and issue["labels"] == ["cardinal:error"] and not state["prs"]
                   and len(pushes) == expected_pushes
                   and (name != "remote moved" or
                        git(bare, "rev-parse", f"refs/heads/{run['branch']}") == git(bare, "rev-parse", "refs/heads/main")),
                   {"result": result, "pushes": pushes})
        if name in ("remote failure", "exhausted"):
            expect(checks, f"{name}: retries are paced and bounded", elapsed >= (0.75 if name == "exhausted" else 0.25),
                   elapsed)
        product.keep_store()
    return {"checks": checks}


def monitor(temp: Path, artifact: Path) -> dict:
    from cardinal_harness.monitor_scenario import scenario  # imports offline; kept local to avoid a cycle
    return scenario(temp, artifact)


def scout(temp: Path, artifact: Path) -> dict:
    from cardinal_harness.scout_scenario import scenario  # imports offline; kept local to avoid a cycle
    return scenario(temp, artifact)


SCENARIOS = {"single": single, "daemon": daemon, "multi-daemon": multi_daemon, "repos-check": repos_check, "ci-failure": ci_failure, "ci-repair": ci_repair, "deploy": deploy,
             "cleaner": cleaner, "monitor": monitor, "base-sync": base_sync, "post-merge": post_merge,
             "parallel": parallel, "daemon-restart": daemon_restart, "worker-killed": worker_killed,
             "scout": scout, "documentation": documentation, "file-tool-refusal": file_tool_refusal,
             "gh-retries": gh_retries, "push-retries": push_retries, "setup-recovery": setup_recovery}


def run(scenario: str, artifact: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"cardinal-offline-{scenario}-") as name:
        report = SCENARIOS[scenario](Path(name), artifact)
    report["scenario"] = scenario
    report["passed"] = all(item["ok"] for item in report["checks"].values())
    return report
