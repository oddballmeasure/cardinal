"""Offline `monitor` scenario: Cardinal's own crash and a target app's repeated error become
triaged issues on the repository that produced each, with no duplicates.

1. `cardinal run 16` crashes in intake while the logs directory is read-only: the run must still
   settle, and the store copy of the record must still reach the monitor.
2. The harness plays the target app and posts records to `cardinal ingest`.
3. Four `cardinal monitor --once` passes: the per-pass cap, triage to investigate and to ready,
   an open finding not filed twice, and a closed finding filed again as a recurrence.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import jsonschema

from cardinal_harness import fake_gh
from cardinal_harness.offline import SLUG, expect, git, world
from cardinal_harness.product import Product, json_file, product_env, write_config

SELF = "example/cardinal"
TOKEN = "offline-ingest-token"


def app_record(level: str = "error", repo: str = SLUG, **overrides) -> dict:
    stack = [{"file": "ledger/__main__.py", "function": "main", "line": 40},
             {"file": "ledger/query.py", "function": "average", "line": 12}]
    return {"level": level, "event": "request_failed", "message": "GET /totals failed",
            "source": {"repo": repo, "component": "ledger-api", "revision": "abc123"},
            "error": {"type": "ZeroDivisionError", "message": "division by zero", "stack": stack,
                      "traceback": "Traceback (most recent call last):\n  File \"ledger/query.py\", line 12, in average\n"
                                   "ZeroDivisionError: division by zero"}, **overrides}


def post(url: str, body: bytes, token: str | None) -> tuple[int, dict]:
    request = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def configure(temp: Path, bare: Path, state_path: Path, artifact: Path) -> Product:
    self_bare = temp / "cardinal.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(bare), str(self_bare)], check=True)
    home = temp / "home"
    write_config(home, models={role: "replay:scripted" for role in
                               ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer", "monitor")},
                 slug=SLUG, remote_url=str(bare), test_command=[sys.executable, "-m", "pytest", "-q", "tests"],
                 required_checks=[], paths_off_limits=[".github/"], deploy=None, source_repo=SELF,
                 sections={"ingest": {"bind": "127.0.0.1:0", "token_env": "CARDINAL_INGEST_TOKEN"},
                           "monitor": {"min_occurrences": 3, "window_hours": 24, "max_issues_per_pass": 1}},
                 extra_repos=["", "[[repos]]", f"slug = {json.dumps(SELF)}", f"remote_url = {json.dumps(str(self_bare))}",
                              'base_branch = "main"', 'test_command = ["true"]', "required_checks = []",
                              "paths_off_limits = []"])
    fake_bin = fake_gh.install(temp / "bin")
    env = product_env({"PATH": f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}",
                       "CARDINAL_FAKE_GH_STATE": str(state_path),
                       "CARDINAL_MODEL_PROVIDER": "cardinal_harness.replay_provider:provider",
                       "CARDINAL_INGEST_TOKEN": TOKEN})
    return Product(home, env, artifact)


def start_ingest(product: Product) -> tuple[subprocess.Popen, str]:
    process = subprocess.Popen([sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home), "ingest"],
                               env=product.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"ingest did not start: {process.stderr.read()[-2000:]}")
    return process, json.loads(line)["listening"]


def by_action(results) -> dict:
    return {item.get("action"): item for item in results} if isinstance(results, list) else {}


def scenario(temp: Path, artifact: Path) -> dict:
    bare, state_path, state = world(temp, ready=[])
    state["other_repos"] = {SELF: {"issues": {}, "labels": []}}
    json_file(state_path, state)
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}

    logs = product.home / "logs"
    logs.mkdir(parents=True)
    logs.chmod(0o555)
    code, result = product("run", "16", "--repo", SLUG)
    logs.chmod(0o755)
    stderr = Path(product.invocations[-1]["log"]).read_text()
    expect(checks, "a crash with unwritable log files still settles the run as a crash",
           code == 1 and ((result or {}).get("failure") or {}).get("kind") == "crash", result)
    expect(checks, "the dropped log file write is reported, not silent", "log jsonl write failed" in stderr)

    ingest, url = start_ingest(product)
    try:
        status, _ = post(url, json.dumps([app_record()]).encode(), None)
        expect(checks, "ingest refuses a request without the token", status == 401, status)
        batch = [app_record(), app_record(), app_record(), app_record(level="warning"),
                 app_record(level="fatal"), app_record(repo="example/unknown")]
        body = "\n".join(json.dumps(item) for item in batch).encode()  # NDJSON
        status, reply = post(url, body, TOKEN)
        expect(checks, "each bad record is rejected alone", status == 200 and reply.get("accepted") == 4
               and [item["index"] for item in reply.get("rejected", [])] == [4, 5], reply)

        code, first = product("monitor", "--once")
        state = json.loads(state_path.read_text())
        own = state["other_repos"][SELF]["issues"]
        expect(checks, "pass 1 files Cardinal's crash on Cardinal's repository and holds the app error at the cap",
               code == 0 and len(own) == 1 and "held_by_cap" in by_action(first) and
               by_action(first).get("filed", {}).get("repo") == SELF, first)
        crash = next(iter(own.values()), {})
        expect(checks, "the crash triages to investigate with the orchestrator's reason",
               crash.get("labels") == ["cardinal:investigate"] and any("needs_human" in c for c in crash.get("comments", [])),
               crash)
        expect(checks, "the crash issue carries its run and fingerprint",
               "Cardinal-Fingerprint:" in crash.get("body", "") and "Runs: " in crash.get("body", ""), crash.get("body"))

        code, second = product("monitor", "--once")
        state = json.loads(state_path.read_text())
        filed = [issue for issue in state["issues"].values() if issue["title"].startswith("Ledger totals crash")]
        expect(checks, "pass 2 files the app error on the app's repository, triaged ready",
               code == 0 and len(filed) == 1 and filed[0]["labels"] == ["cardinal:ready"], second)
        expect(checks, "the app issue quotes the traceback it was sent",
               bool(filed) and "ZeroDivisionError: division by zero" in filed[0]["body"])
        expect(checks, "Cardinal's own repository still has one issue", len(state["other_repos"][SELF]["issues"]) == 1)

        post(url, json.dumps([app_record()] * 3).encode(), TOKEN)
        code, third = product("monitor", "--once")
        state = json.loads(state_path.read_text())
        again = [issue for issue in state["issues"].values() if issue["title"].startswith("Ledger totals crash")]
        expect(checks, "an open finding is not filed twice", len(again) == 1 and "already_open" in by_action(third), third)

        number = str(filed[0]["number"]) if filed else "0"
        state["issues"][number]["state"] = "CLOSED"
        json_file(state_path, state)
        post(url, json.dumps([app_record()] * 3).encode(), TOKEN)
        code, fourth = product("monitor", "--once")
        state = json.loads(state_path.read_text())
        recurred = [issue for issue in state["issues"].values()
                    if issue["title"].startswith("Ledger totals crash") and issue["state"] == "OPEN"]
        expect(checks, "a closed finding that recurs is filed again, linking the old issue",
               len(recurred) == 1 and f"Recurs after #{number}" in recurred[0]["body"], fourth)
    finally:
        ingest.terminate()
        ingest.wait(timeout=30)

    code, schema = product("logs", "schema")
    lines = [json.loads(line) for path in sorted(logs.glob("*.jsonl")) for line in path.read_text().splitlines()]
    invalid = []
    for line in lines:
        try:
            jsonschema.validate(line, schema)
        except jsonschema.ValidationError as exc:
            invalid.append(exc.message)
    expect(checks, "every JSONL line matches the published schema", code == 0 and lines and not invalid,
           {"lines": len(lines), "invalid": invalid[:5]})
    triage = [line for line in lines if line["event"] == "issue_triaged"]
    expect(checks, "triage records carry their run and stage",
           triage and all(line["context"].get("run_id") and line["context"].get("stage") == "triage" for line in triage),
           triage[:2])
    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}
