"""Live issue propagation: an error in the running test app becomes an issue Cardinal fixes.

1. Start `cardinal ingest`, run the test repository's app at the pinned baseline with reporting
   pointed at it, and call the planted failing endpoint until it has failed `requests` times.
2. `cardinal monitor --once` must file one issue on the test repository and triage it ready.
3. `cardinal daemon --once` must resolve that issue: merged PR, green CI on the verified head,
   the issue closed with Cardinal's comment, and the hidden acceptance tests passing on the new main.
Cleanup resets main to the baseline, which restores the planted bug, so the run repeats as is.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cardinal_harness.github_setup import load_model_key, prepare_test_repo, use_github_cli_git_credentials, wait_baseline_ci
from cardinal_harness.live import (NOTES_SUITE, ROLES, Target, acceptance, cleanup, environment_preflight, exclusive,
                                   fresh_clone, gh, gh_json, grade_case, park_ready, remote_main, wait_ready)
from cardinal_harness.product import FIXTURES, ROOT, Product, json_file, product_env, write_config

TOKEN = uuid.uuid4().hex
READY = "cardinal:ready"
TRAILER = "Cardinal-Fingerprint:"


def start_ingest(product: Product) -> tuple[subprocess.Popen, int]:
    process = subprocess.Popen([sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home), "ingest"],
                               env=product.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"cardinal ingest did not start: {process.stderr.read()[-2000:]}")
    return process, int(json.loads(line)["listening"].rsplit(":", 1)[1].split("/")[0])


def provoke(url: str, temp: Path, case: dict, port: int) -> list[int]:
    """Run the app from a fresh clone with reporting on, and call the failing endpoint."""
    repo = fresh_clone(url, temp / "app")
    project = f"cardinal-propagate-{uuid.uuid4().hex[:8]}"
    compose = ["docker", "compose", "-p", project, "-f", str(repo / "compose.yaml")]
    env = {**os.environ, "CARDINAL_INGEST_URL": f"http://host.docker.internal:{port}/v1/records",
           "CARDINAL_INGEST_TOKEN": TOKEN}
    try:
        subprocess.run([*compose, "up", "--build", "--detach", "--wait", "--wait-timeout", "240"], cwd=repo, env=env,
                       text=True, capture_output=True, check=True, timeout=360)
        address = subprocess.run([*compose, "port", "api", "8000"], cwd=repo, text=True, capture_output=True,
                                 check=True, timeout=15).stdout.strip().splitlines()[-1].rsplit(":", 1)[1]
        statuses = []
        for _ in range(case["requests"]):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{address}{case['endpoint']}", timeout=10) as response:
                    statuses.append(response.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)
        time.sleep(3)  # the app reports on a background thread
        return statuses
    finally:
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], cwd=repo, capture_output=True,
                       check=False, timeout=120)


def received(home: Path, repository: str, frame: str, wanted: int, timeout: float = 30) -> list[dict]:
    """App records as Cardinal stored them in its JSONL log, read from outside the product."""
    deadline = time.monotonic() + timeout
    while True:
        found = [record for path in sorted((home / "logs").glob("*.jsonl"))
                 for record in map(json.loads, path.read_text().splitlines())
                 if record["source"]["repo"] == repository and record["event"] == "request_failed"
                 and any(item["file"] == frame for item in (record.get("error") or {}).get("stack", []))]
        if len(found) >= wanted or time.monotonic() >= deadline:
            return found
        time.sleep(1)


def close_stale(repository: str, note: str) -> list[int]:
    """Monitor-filed issues from earlier runs, identified by the trailer only the monitor writes."""
    stale = [item["number"] for item in gh_json("issue", "list", "-R", repository, "--state", "open", "--limit", "100",
                                                "--search", f'"{TRAILER}" in:body', "--json", "number,body")
             if TRAILER in item["body"]]
    for number in stale:
        gh("issue", "close", str(number), "-R", repository, "--comment", note)
    return stale


def run(model: str, artifact: Path) -> dict:
    case = json.loads((FIXTURES / "monitor_case.json").read_text())
    target = Target.of(json.loads(NOTES_SUITE.read_text()))
    repository, url = case["repository"], f"https://github.com/{case['repository']}"
    report: dict = {"case": case["key"], "model": model, "checks": {}, "passed": False}
    checks = report["checks"]
    parked: list[dict] = []
    baseline = product = ingest = None
    filed: int | None = None

    def expect(name: str, ok: bool, detail: object = None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}
        json_file(artifact / "report.json", report)

    with tempfile.TemporaryDirectory(prefix="cardinal-propagate-") as name:
        temp = Path(name)
        try:
            load_model_key()
            use_github_cli_git_credentials()
            report["test_repo_preflight"] = prepare_test_repo(url, target.checkout)
            baseline = remote_main(url)
            report["baseline_sha"] = baseline
            if case["baseline_sha"] != baseline:
                raise ValueError(f"Remote main {baseline} differs from the pinned baseline {case['baseline_sha']}")
            wait_baseline_ci(repository, baseline, artifact, target.check)
            preflight = environment_preflight(url, temp, target)
            report["environment_preflight"] = preflight
            if preflight["exit_code"] != 0:
                raise ValueError("The test repository's own suite fails at the baseline on this machine")
            before = acceptance(url, temp, case, "before")
            if before["exit_code"] == 0:
                raise ValueError("The planted bug is already fixed on the baseline")
            report["stale_issues_closed"] = close_stale(repository, "Closed by the propagation harness before a new run.")
            parked.extend(park_ready(repository, READY))
            if not parked:
                raise ValueError("No ready suite issues to park; cleanup restores them and needs at least one")
            json_file(artifact / "parked_issues.json", parked)

            home = temp / "home"
            write_config(home, models={role: model for role in ROLES}, slug=repository, remote_url=url,
                         test_command=target.test_command, required_checks=[target.check],
                         paths_off_limits=target.paths_off_limits, deploy=target.deploy,
                         sections={"ingest": {"bind": "127.0.0.1:0", "token_env": "CARDINAL_INGEST_TOKEN"},
                                   "monitor": {"min_occurrences": case["requests"], "window_hours": 1,
                                               "max_issues_per_pass": 3}})
            product = Product(home, product_env({
                "OPENAI_API_KEY": os.environ["OPENAI_API_KEY"], "CARDINAL_INGEST_TOKEN": TOKEN,
                "CARDINAL_DEPLOY_HOST": "cardinal_harness.mock_host:factory",
                "CARDINAL_MOCK_HOST_FIXTURE": str(FIXTURES / "deploy_hosts" / "default.json"),
            }), artifact)
            code, repo_check = product("repos", "check")
            if code != 0:
                raise ValueError(f"cardinal repos check failed: {repo_check}")

            ingest, port = start_ingest(product)
            statuses = provoke(url, temp, case, port)
            expect("the planted endpoint fails in the running app", statuses == [500] * case["requests"], statuses)
            records = received(home, repository, case["expected_frame"], case["requests"])
            expect("every app error reached Cardinal with its stack", len(records) == case["requests"],
                   [record["error"]["stack"] for record in records])

            code, results = product("monitor", "--once")
            filings = [item for item in results or [] if item.get("action") == "filed"] if isinstance(results, list) else []
            ours = [item for item in filings if item.get("repo") == repository]
            filed = ours[0]["issue"] if len(ours) == 1 else None
            expect("the monitor filed one issue on the test repository", code == 0 and filed is not None, results)
            if filed is None:
                raise ValueError("No issue was filed; nothing for the daemon to resolve")
            issue = gh_json("issue", "view", str(filed), "-R", repository, "--json", "title,body,labels,state")
            expect("the issue carries the fingerprint and the failing frame",
                   TRAILER in issue["body"] and case["expected_frame"] in issue["body"], issue["body"][-3000:])
            expect("triage marked the issue ready", ours[0].get("label") == READY, ours[0])
            if ours[0].get("label") != READY:
                raise ValueError("The orchestrator did not triage the issue as workable")

            wait_ready(repository, READY, [filed])
            code, runs = product("daemon", "--once")
            result = next((item for item in runs or [] if item.get("issue") == filed), {}) if isinstance(runs, list) else {}
            graded = grade_case(product, {**case, "issue": {"number": filed}}, url, repository, temp, [], result, target)
            for name, item in graded["checks"].items():
                expect(name, item["ok"], item["detail"])
            report["usage"] = graded["usage"]
            final = gh_json("issue", "view", str(filed), "-R", repository, "--json", "state,labels,comments")
            pr_number = graded["run"].get("pr_number")
            pr = gh_json("pr", "view", str(pr_number), "-R", repository, "--json", "body,url") if pr_number else {}
            expect("the PR closes the filed issue", f"closes #{filed}" in pr.get("body", "").lower(), pr.get("body"))
            expect("the issue is closed, labelled done, and names the PR",
                   final["state"] == "CLOSED" and [label["name"] for label in final["labels"]] == ["cardinal:done"]
                   and any(pr.get("url", "-") in comment["body"] for comment in final["comments"]),
                   {"state": final["state"], "labels": final["labels"],
                    "comments": [comment["body"][:300] for comment in final["comments"]]})
            report["workflow_passed"] = all(item["ok"] for item in checks.values())
        except Exception as exc:  # noqa: BLE001 - recorded, then cleanup still runs
            report["error"] = f"{type(exc).__name__}: {exc}"
            (artifact / "error.txt").write_text(traceback.format_exc())
        finally:
            if ingest is not None:
                ingest.terminate()
                ingest.wait(timeout=30)
            if product is not None:
                product.keep_store()
                report["invocations"] = product.invocations
            if filed is not None:
                state = gh_json("issue", "view", str(filed), "-R", repository, "--json", "state")["state"]
                if state == "OPEN":
                    gh("issue", "close", str(filed), "-R", repository, "--comment", "Closed by the propagation harness cleanup.")
            if baseline and parked:
                report["cleanup"] = cleanup(temp, repository, url, baseline, parked, product, model, artifact)
    report["passed"] = bool(report.get("workflow_passed") and (report.get("cleanup") or {}).get("clean"))
    json_file(artifact / "report.json", report)
    return report


def run_exclusive(model: str, artifact: Path) -> dict:
    return exclusive(run, model, artifact)
