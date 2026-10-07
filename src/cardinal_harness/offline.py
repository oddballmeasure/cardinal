"""Offline end-to-end scenarios: the real product against a bare Git remote and a fake `gh`.

`single`: ledger issue 104 through `cardinal run`: two dependent tickets, CI pending then success,
          a merge, and a deploy whose health becomes ready only on the second poll.
`daemon`: four issues through `cardinal daemon --once`: #12 builds on #11's merge, #13 asks a
          person and is then declined via `cardinal resume`, #14 never passes its tests.
`base-sync`: other merges land on main while #11 and #12 are worked; conflicts and a clean move are
          merged in, resolved, verified again and merged.
`post-merge`: checks that fail on merge commits are re-run once; a repeat failure files a follow-up.
Every pass is judged from outside: fake-GitHub state, the bare remote's refs, `status --json`,
and the independent acceptance tests in tests/acceptance.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
              env: dict[str, str] | None = None) -> Product:
    home = temp / "home"
    write_config(home, models={role: "replay:scripted" for role in
                               ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer", "monitor")},
                 slug=SLUG, remote_url=str(bare), test_command=[sys.executable, "-m", "pytest", "-q", "tests"],
                 required_checks=required_checks or [CHECK], paths_off_limits=[".github/"], deploy="local",
                 limits={"coder_attempts": 2, "ci_poll_seconds": 1, "ci_timeout_seconds": 120})
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


def monitor(temp: Path, artifact: Path) -> dict:
    from cardinal_harness.monitor_scenario import scenario  # imports offline; kept local to avoid a cycle
    return scenario(temp, artifact)


SCENARIOS = {"single": single, "daemon": daemon, "ci-failure": ci_failure, "ci-repair": ci_repair, "deploy": deploy,
             "cleaner": cleaner, "monitor": monitor, "base-sync": base_sync, "post-merge": post_merge}


def run(scenario: str, artifact: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"cardinal-offline-{scenario}-") as name:
        report = SCENARIOS[scenario](Path(name), artifact)
    report["scenario"] = scenario
    report["passed"] = all(item["ok"] for item in report["checks"].values())
    return report
