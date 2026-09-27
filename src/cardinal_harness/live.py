"""The live GitHub suite, driven through the product's daemon one case at a time.

For each case, in suite order:
  1. prove the case's acceptance tests fail on current main (the issue is not already solved);
  2. label it ready, and run `cardinal daemon --once`;
  3. grade from outside: the run is done, the PR merged the head Cardinal verified, the required
     check passed on that exact head, the deploy reports the merge commit, and the case's acceptance
     tests pass on the new main. After the last case, every case is re-checked on the final main.
A failed case stops the suite, since later cases build on earlier merges. Cleanup always runs:
the pinned base is restored under an exact lease and every managed PR, branch and label reverts.
"""

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from langchain.chat_models import init_chat_model

from cardinal_harness import cleanup as cleaner_agent
from cardinal_harness.acceptance import run_checks
from cardinal_harness.contracts import CleanupIssue, CleanupRequest
from cardinal_harness.github_setup import (discover_cases, load_model_key, prepare_test_repo,
                                           use_github_cli_git_credentials, wait_baseline_ci)
from cardinal_harness.product import FIXTURES, ROOT, Product, json_file, product_env, write_config

CHECK = "http-e2e"
ROLES = ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer")


def gh_json(*args: str):
    return json.loads(subprocess.run(["gh", *args], text=True, capture_output=True, check=True, timeout=60).stdout)


def gh(*args: str) -> None:
    subprocess.run(["gh", *args], text=True, capture_output=True, check=True, timeout=60)


def remote_main(url: str) -> str:
    return subprocess.run(["git", "ls-remote", url, "refs/heads/main"], text=True, capture_output=True,
                          check=True, timeout=60).stdout.split()[0]


def fresh_clone(url: str, into: Path) -> Path:
    subprocess.run(["git", "clone", "-q", url, str(into)], check=True, capture_output=True, timeout=300)
    return into


def acceptance(url: str, temp: Path, case: dict, label: str) -> dict:
    """Clone main fresh for every check; each grading round re-checks earlier cases on a newer main."""
    work = Path(tempfile.mkdtemp(prefix=f"accept-{case['key']}-{label}-", dir=temp))
    repo = fresh_clone(url, work / "repo")
    try:
        evidence = run_checks(repo, case["requirement_ids"], case, ROOT / case["acceptance"])
    finally:
        shutil.rmtree(work, ignore_errors=False)
    return {"exit_code": evidence.exit_code, "passed_tests": evidence.passed_tests,
            "expected": len(case["requirement_ids"]), "stdout": evidence.stdout[-3000:]}


def environment_preflight(url: str, temp: Path) -> dict:
    """Run the test repository's own suite at the baseline, where it must pass. A failure here is the
    machine (Docker, browsers, network), not Cardinal (lessons/docker-credential-helper-hang.md)."""
    repo = fresh_clone(url, temp / "preflight")
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=repo,
                            text=True, capture_output=True, check=False, timeout=1200)
    return {"exit_code": result.returncode, "stdout": result.stdout[-3000:], "stderr": result.stderr[-2000:]}


def check_run(repository: str, sha: str | None) -> str | None:
    if not sha:
        return None
    try:
        found = gh_json("api", f"repos/{repository}/commits/{sha}/check-runs")["check_runs"]
    except subprocess.CalledProcessError:  # GitHub has never seen this commit: it was not pushed
        return None
    runs = [run for run in found if run["name"] == CHECK and run["head_sha"] == sha]
    return max(runs, key=lambda run: run["id"])["conclusion"] if runs else None


def grade_case(product: Product, case: dict, url: str, repository: str, temp: Path, solved: list[dict], result: dict) -> dict:
    number = case["issue"]["number"]
    checks: dict = {}

    def expect(name: str, ok: bool, detail: object = None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    expect("run settled done", result.get("status") == "done", result)
    run = (product.status(number) or [{}])[-1]
    if result.get("status") != "done":  # the rest needs a merge; don't spend Docker minutes proving its absence
        return {"issue": number, "key": case["key"], "usage": {}, "checks": checks, "passed": False,
                "run": {key: value for key, value in run.items() if key not in {"events", "agent_calls"}}}
    main = remote_main(url)
    expect("merge is the new main", run.get("merge_sha") == main, {"run": run.get("merge_sha"), "main": main})
    pr = gh_json("pr", "view", str(run.get("pr_number") or 0), "-R", repository,
                 "--json", "state,headRefOid,mergeCommit") if run.get("pr_number") else {}
    expect("PR merged the verified head", pr.get("state") == "MERGED" and pr.get("headRefOid") == run.get("head_sha"),
           {"pr": pr, "verified": run.get("head_sha")})
    expect(f"{CHECK} passed on the verified head", check_run(repository, run.get("head_sha")) == "success")
    expect("deployed revision is the merge", run.get("deployed_sha") == main, run.get("deployed_sha"))
    after = acceptance(url, temp, case, "after")
    expect("acceptance passes on the new main", after["exit_code"] == 0 and after["passed_tests"] == after["expected"], after)
    calls = run.get("agent_calls", [])
    usage = {"input_tokens": sum(call["input_tokens"] for call in calls),
             "output_tokens": sum(call["output_tokens"] for call in calls),
             "agent_seconds": round(sum(call["seconds"] for call in calls)),
             "agent_calls": len(calls)}
    return {"issue": number, "key": case["key"], "usage": usage,
            "run": {key: value for key, value in run.items() if key not in {"events", "agent_calls"}},
            "checks": checks, "passed": all(item["ok"] for item in checks.values())}


def run_suite(suite_path: Path, model: str, artifact: Path, only: list[str] | None) -> dict:
    """Only one live suite may touch the shared test repository at a time."""
    lock = (ROOT / "artifacts" / "live.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"suite": str(suite_path), "passed": False, "cases": [],
                "error": "Another live suite holds artifacts/live.lock; it would share the test repository"}
    try:
        return run_locked(suite_path, model, artifact, only)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def run_locked(suite_path: Path, model: str, artifact: Path, only: list[str] | None) -> dict:
    suite = json.loads(suite_path.read_text())
    repository, url = suite["repository"], f"https://github.com/{suite['repository']}"
    report: dict = {"suite": str(suite_path), "model": model, "cases": [], "passed": False}
    discovered: list[dict] = []
    parked: list[dict] = []
    baseline = None
    product: Product | None = None
    with tempfile.TemporaryDirectory(prefix="cardinal-live-") as name:
        temp = Path(name)
        try:
            load_model_key()
            use_github_cli_git_credentials()
            report["test_repo_preflight"] = prepare_test_repo(url, ROOT / "tests" / "blank_repo")
            json_file(artifact / "test_repo_preflight.json", report["test_repo_preflight"])
            probe = init_chat_model(model).invoke("Reply with OK only.")
            if str(probe.content).strip() != "OK":
                raise ValueError(f"Model probe returned {probe.content!r}")
            baseline = remote_main(url)
            report["baseline_sha"] = baseline
            if suite.get("baseline_sha") and suite["baseline_sha"] != baseline:
                raise ValueError("Remote main differs from the suite's pinned baseline")
            wait_baseline_ci(repository, baseline, artifact)
            preflight = environment_preflight(url, temp)
            report["environment_preflight"] = preflight
            if preflight["exit_code"] != 0:
                raise ValueError("The test repository's own suite fails at the baseline on this machine; "
                                 "fix the environment before spending model calls")
            cases = [case for case in suite["cases"] if not only or case["key"] in only]
            discovered = discover_cases(repository, cases, suite["ready_label"])
            json_file(artifact / "discovered_issues.json", [{"key": item["key"], "issue": item["issue"]["number"],
                                                             "labels": item["original_labels"]} for item in discovered])
            # Nothing is ready until the harness says so, one case at a time. Every open ready issue is
            # parked, not only the selected cases, and every parked issue is restored by cleanup.
            parked.extend(park_ready(repository, suite["ready_label"]))
            json_file(artifact / "parked_issues.json", parked)
            home = temp / "home"
            write_config(home, models={role: model for role in ROLES}, slug=repository, remote_url=url,
                         test_command=[sys.executable, "-m", "pytest", "-q", "tests"], required_checks=[CHECK],
                         paths_off_limits=[".github/"], deploy="local")
            product = Product(home, product_env({
                "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
                "CARDINAL_DEPLOY_HOST": "cardinal_harness.mock_host:factory",
                "CARDINAL_MOCK_HOST_FIXTURE": str(FIXTURES / "deploy_hosts" / "default.json"),
            }), artifact)
            code, repo_check = product("repos", "check")
            report["repos_check"] = repo_check
            if code != 0:
                raise ValueError(f"cardinal repos check failed: {repo_check}")
            solved: list[dict] = []
            for case in discovered:
                number = case["issue"]["number"]
                before = acceptance(url, temp, case, "before")
                if before["exit_code"] == 0:
                    raise ValueError(f"{case['key']} already passes its acceptance tests before Cardinal ran")
                gh("issue", "edit", str(number), "-R", repository, "--add-label", suite["ready_label"])
                wait_ready(repository, suite["ready_label"], [number])
                code, results = product("daemon", "--once")
                result = next((item for item in results or [] if item.get("issue") == number), {}) \
                    if isinstance(results, list) else {}
                graded = grade_case(product, case, url, repository, temp, solved, result)
                graded["baseline_acceptance"] = before
                report["cases"].append(graded)
                json_file(artifact / "report.json", report)
                if not graded["passed"]:
                    raise ValueError(f"Case {case['key']} (#{number}) failed: "
                                     + ", ".join(name for name, item in graded["checks"].items() if not item["ok"]))
                solved.append(case)
            # Every earlier issue must still hold on the final main: a later merge may have regressed one.
            final = {case["key"]: acceptance(url, temp, case, "final") for case in solved}
            report["final_acceptance"] = final
            json_file(artifact / "report.json", report)
            regressed = [key for key, item in final.items()
                         if item["exit_code"] != 0 or item["passed_tests"] != item["expected"]]
            if regressed:
                raise ValueError(f"Cases regressed on the final main: {regressed}")
            report["workflow_passed"] = True
        except Exception as exc:  # noqa: BLE001 - recorded, then cleanup still runs
            report["error"] = f"{type(exc).__name__}: {exc}"
            (artifact / "error.txt").write_text(traceback.format_exc())
        finally:
            if product is not None:
                product.keep_store()
                report["invocations"] = product.invocations
            if baseline and parked:
                report["cleanup"] = cleanup(temp, repository, url, baseline, parked, product, model, artifact)
    report["passed"] = bool(report.get("workflow_passed") and (report.get("cleanup") or {}).get("clean"))
    json_file(artifact / "report.json", report)
    scoreboard(report, artifact, model)
    return report


def product_version() -> str:
    """Content hash of the product source and skills: the unit whose improvement the suite measures."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src" / "cardinal").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(ROOT)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:16]


def scoreboard(report: dict, artifact: Path, model: str) -> None:
    """One line per graded case in artifacts/scoreboard.jsonl, comparable across product versions."""
    version = product_version()
    with (ROOT / "artifacts" / "scoreboard.jsonl").open("a") as board:
        for case in report["cases"]:
            board.write(json.dumps({"run": artifact.name, "product": version, "model": model, "case": case["key"],
                                    "passed": case["passed"], **case.get("usage", {})}) + "\n")


def park_ready(repository: str, label: str) -> list[dict]:
    """Remove the ready label from every open issue, returning each issue's original labels."""
    parked = []
    for item in gh_json("issue", "list", "-R", repository, "--state", "open", "--label", label,
                        "--limit", "100", "--json", "number,labels"):
        parked.append({"number": item["number"], "labels": [entry["name"] for entry in item["labels"]]})
        gh("issue", "edit", str(item["number"]), "-R", repository, "--remove-label", label)
    wait_ready(repository, label, [])
    return parked


def wait_ready(repository: str, label: str, expected: list[int], timeout: float = 180) -> None:
    """GitHub's label listing lags a label edit; start the daemon only once it shows exactly `expected`."""
    deadline = time.monotonic() + timeout
    while True:
        seen = sorted(item["number"] for item in gh_json("issue", "list", "-R", repository, "--state", "open",
                                                          "--label", label, "--limit", "100", "--json", "number"))
        if seen == sorted(expected):
            return
        if time.monotonic() >= deadline:
            raise ValueError(f"Ready issues are {seen}, expected {expected}")
        time.sleep(5)


def cleanup(temp: Path, repository: str, url: str, baseline: str, parked: list[dict], product, model: str, artifact: Path) -> dict:
    try:
        rows = product.status() if product else []
        branches = sorted({row["branch"] for row in rows if row.get("branch")})
        prs = sorted({row["pr_number"] for row in rows if row.get("pr_number")})
        request = CleanupRequest(
            repository=repository, remote_url=url, base_branch="main", base_sha=baseline,
            expected_head_sha=remote_main(url), managed_branches=branches, managed_pr_numbers=prs,
            issue_action="reopen", issue_number=parked[0]["number"],
            issue_resets=[CleanupIssue(action="reopen", number=item["number"], labels=item["labels"]) for item in parked],
        )
        return cleaner_agent.github_clean(temp / "cleaner", request, model, artifact)
    except Exception as exc:  # noqa: BLE001
        (artifact / "cleanup_error.txt").write_text(traceback.format_exc())
        return {"clean": False, "error": f"{type(exc).__name__}: {exc}"}


def clean_from_manifest(manifest: Path, model: str, artifact: Path) -> dict:
    """Restore the live repository from a pinned CleanupRequest, e.g. after an interrupted suite."""
    load_model_key()
    use_github_cli_git_credentials()
    request = CleanupRequest.model_validate_json(manifest.read_text())
    with tempfile.TemporaryDirectory(prefix="cardinal-clean-") as name:
        result = cleaner_agent.github_clean(Path(name), request, model, artifact)
    return {"cleanup": result, "passed": bool(result.get("clean"))}
