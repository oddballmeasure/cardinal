"""Offline `scout` scenario: `cardinal scout --once` surveys the ledger fixture and files reviewed,
code-checked proposals, which the daemon leaves alone until a person approves them.

Pass 1 (propose, cap 2): a real defect is filed with its evidence; a fabricated quote, a duplicate
of an open issue and a reviewer refusal are dropped; the overflow is held. A person then approves
one proposal (label swap) and rejects the other (close with a comment). Pass 2 reads both back,
hands the rejection reason to its survey, rotates to the area not yet surveyed and files the held
product decision for a person. It runs with autonomy "auto" over a seeded record of landed bugs:
a bug with a failing repro test goes straight to ready, a feature below its bars stays proposed,
and a bug whose "failing" test passes on base is dropped. Pass 3 reads the approved issue's
landing as done.

The repository sets focused_test_command, and every test-command invocation is recorded: main moves
between passes 1 and 2, so each pass that reproduces bugs runs the full suite once for its base, and
each repro runs only its own test, by path and name.
"""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from cardinal_harness import fake_gh, scout_replay
from cardinal_harness.offline import SLUG, expect, git, world
from cardinal_harness.product import Product, json_file, product_env, run_bounded, write_config

ROLES = ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer", "monitor", "scout", "scout_reviewer")
SCOUT = {"autonomy": "propose", "categories": ["bug", "feature"], "areas_per_pass": 2, "cooldown_days": 7,
         "max_proposals_per_pass": 2, "max_files": 3, "repro": True, "auto_min_decided": 3,
         "auto_min_approval": 0.8, "auto_min_done": 0.7}


REJECTION = "Not wanted: this CLI only filters by merchant; amount filters belong in the report tool."


FULL = ["-q", "tests"]
FOCUSED = ["-q", "{test_path}::{test_name}"]
RECORDER = '''import json
import os
import sys

with open({log!r}, "a") as output:
    output.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
os.execv(sys.executable, [sys.executable, "-m", "pytest", *sys.argv[1:]])
'''


def recorder(home: Path) -> Path:
    """A test runner that appends each invocation's argv and directory to a log, then runs pytest."""
    script = home.parent / "test-runner" / "record_tests.py"
    if not script.exists():
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(RECORDER.format(log=str(script.parent / "invocations.jsonl")))
    return script


def recorded_runs(home: Path) -> list[dict]:
    log = recorder(home).parent / "invocations.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def write(home: Path, bare: Path, scout: dict) -> None:
    runner = [sys.executable, str(recorder(home))]
    write_config(home, models={role: "replay:scripted" for role in ROLES}, slug=SLUG, remote_url=str(bare),
                 test_command=[*runner, *FULL], focused_test_command=[*runner, *FOCUSED], required_checks=[],
                 paths_off_limits=[".github/"], deploy=None, sections={"scout": scout})


def check_test_runs(checks: dict, passes: list[tuple[str, list[dict], list[str]]]) -> None:
    """From recorded invocations: each pass ran the full test command exactly once, outside any repro
    worktree, and each repro worktree ran only the focused command, filled from its ReproResult, twice:
    the agent's own run and the confirming one."""
    full, focused, ran_once = {}, {}, True
    for name, runs, titles in passes:
        repro = [run for run in runs if "-repro-" in Path(run["cwd"]).name]
        full[name] = [run for run in runs if run not in repro]
        # A pass with no bug to reproduce never needs the base-green run.
        ran_once &= [run["argv"] for run in full[name]] == ([FULL] if titles else [])
        groups: dict[str, list] = {}
        for run in repro:
            groups.setdefault(run["cwd"], []).append(run["argv"])
        expected = sorted([["-q", f"{path}::{test}"]] * 2 for path, test, _ in (scout_replay.REPRO_TESTS[t] for t in titles))
        focused[name] = {"groups": groups, "expected": expected, "ok": sorted(groups.values()) == expected}
    expect(checks, "from recorded invocations, the full test command ran exactly once per pass that reproduces bugs, "
           "for base-green, and not in a pass with nothing to reproduce", ran_once and any(full.values()), full)
    expect(checks, "from recorded invocations, each repro ran only the focused command with its test path and name",
           all(item["ok"] for item in focused.values()), focused)


def configure(temp: Path, bare: Path, state_path: Path, artifact: Path) -> Product:
    write(temp / "home", bare, SCOUT)
    fake_bin = fake_gh.install(temp / "bin")
    env = product_env({"PATH": f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}",
                       "CARDINAL_FAKE_GH_STATE": str(state_path),
                       "CARDINAL_MODEL_PROVIDER": "cardinal_harness.replay_provider:provider"})
    return Product(temp / "home", env, artifact)


def by_title(result) -> dict:
    return {item["title"]: item for item in result.get("proposals", [])} if isinstance(result, dict) else {}


def filed(state: dict, title: str) -> dict:
    return next((issue for issue in state["issues"].values() if issue["title"] == title), {})


def scenario(temp: Path, artifact: Path) -> dict:
    bare, state_path, _ = world(temp, ready=[])
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}

    code, first = product("scout", "--once", "--repo", SLUG)
    first_runs = recorded_runs(product.home)
    seen = by_title(first)
    state = json.loads(state_path.read_text())
    real = filed(state, scout_replay.MISSING_INPUT.title)
    expect(checks, "pass 1 surveys the first two areas", code == 0 and isinstance(first, dict)
           and first.get("areas") == ["input loading", "merchant filter"], first)
    expect(checks, "the real defect is filed as proposed with its checked evidence",
           real.get("labels") == ["cardinal:proposed"] and "`ledger/records.py:8`" in real.get("body", "")
           and "return json.loads(path.read_text())" in real.get("body", "") and "Cardinal-Scout: " in real.get("body", ""),
           real)
    expect(checks, "the defect's body carries the repro test that failed on base",
           "## Suggested test" in real.get("body", "") and "+def test_missing_input_is_reported" in real.get("body", ""),
           real.get("body"))
    fabricated = seen.get(scout_replay.FABRICATED.title, {})
    expect(checks, "a fabricated quote is dropped by code before review",
           fabricated.get("action") == "dropped" and "does not contain the quoted text" in fabricated.get("reason", "")
           and "verdict" not in fabricated, fabricated)
    duplicate = seen.get(scout_replay.DUPLICATE.title, {})
    expect(checks, "a duplicate of open issue #14 is suppressed",
           duplicate.get("action") == "dropped" and "#14" in duplicate.get("reason", ""), duplicate)
    wrong = seen.get(scout_replay.CASE.title, {})
    expect(checks, "a proposal the reviewer finds wrong is dropped",
           wrong.get("action") == "dropped" and wrong.get("verdict") == "wrong", wrong)
    proposed = [issue for issue in state["issues"].values() if "cardinal:proposed" in issue["labels"]]
    held = seen.get(scout_replay.ACCENTS.title, {})
    expect(checks, "the cap files two and holds the overflow unreviewed",
           len(proposed) == 2 and held.get("action") == "held" and "verdict" not in held, first)

    code, drained = product("daemon", "--once", "--repo", SLUG)
    after = json.loads(state_path.read_text())
    expect(checks, "the daemon never picks up proposed issues", code == 0 and drained == []
           and all(after["issues"][str(issue["number"])]["labels"] == ["cardinal:proposed"] for issue in proposed), drained)

    approved, rejected = filed(after, scout_replay.MISSING_INPUT.title), filed(after, scout_replay.MIN_AMOUNT.title)
    after["issues"][str(approved.get("number"))]["labels"] = ["cardinal:ready"]
    after["issues"][str(rejected.get("number"))].update(state="CLOSED", comments=[REJECTION])
    json_file(state_path, after)

    with sqlite3.connect(product.home / "store.db") as db:  # a track record from earlier passes: bugs landed
        for number in range(3):
            db.execute("INSERT INTO scout_proposals (repo, pass_id, fingerprint, category, title, proposal, status,"
                       " filed_mode, issue, outcome, created_at, decided_at) VALUES (?, 'seed', ?, 'bug', ?, '{}',"
                       " 'filed', 'proposed', ?, 'done', datetime('now'), strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))",
                       (SLUG, f"seed-{number}", f"Seeded bug {number}", 900 + number))
    # Main moves on, so pass 2 has a base of its own that has not been proven green yet.
    seed = temp / "seed"
    with (seed / "README.md").open("a") as readme:
        readme.write("\nRun `python -m ledger --help` for its options.\n")
    git(seed, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qam", "Mention --help")
    git(seed, "push", "-q", str(bare), "HEAD:main")
    write(product.home, bare, {**SCOUT, "autonomy": "auto", "max_proposals_per_pass": 5})
    before = len(recorded_runs(product.home))
    code, second = product("scout", "--once", "--repo", SLUG)
    second_runs = recorded_runs(product.home)[before:]
    state = json.loads(state_path.read_text())
    read_back = {item["issue"]: item for item in second.get("outcomes", [])} if isinstance(second, dict) else {}
    expect(checks, "pass 2 reads the label swap as approval and the close as rejection, with its comment",
           read_back.get(approved.get("number"), {}).get("outcome") == "approved"
           and read_back.get(rejected.get("number"), {}).get("outcome") == "rejected"
           and read_back.get(rejected.get("number"), {}).get("reason") == REJECTION, read_back)
    context = product.home / "runs" / str((second or {}).get("pass_id")) / "context" / "rejections.json"
    told = json.loads(context.read_text()) if context.is_file() else []
    expect(checks, "the rejection reason is in the next survey's context",
           [item["reason"] for item in told] == [REJECTION], told)
    expect(checks, "pass 2 rotates to the area not yet surveyed", code == 0 and isinstance(second, dict)
           and second.get("areas") == ["CLI entry point"], second)
    decision = filed(state, scout_replay.ACCENTS.title)
    second_seen = by_title(second)
    unknown = second_seen.get(scout_replay.UNKNOWN.title, {})
    expect(checks, "a bug whose new test passes on base is dropped as not reproduced",
           unknown.get("action") == "dropped" and unknown.get("repro_ok") == 0
           and "did not reproduce" in unknown.get("reason", "") and not filed(state, scout_replay.UNKNOWN.title), unknown)
    malformed, count = filed(state, scout_replay.MALFORMED.title), filed(state, scout_replay.COUNT.title)
    relocated = filed(state, scout_replay.MISCOUNTED.title)
    corrected = second_seen.get(scout_replay.MISCOUNTED.title, {})
    with sqlite3.connect(product.home / "store.db") as db:
        row = db.execute("SELECT proposal FROM scout_proposals WHERE title = ?",
                         (scout_replay.MISCOUNTED.title,)).fetchone()
    stored = json.loads(row[0])["evidence"][0] if row else {}
    expect(checks, "a unique one-line quote 25 lines from a three-line citation is filed and corrected",
           corrected.get("action") == "filed" and stored.get("line_start") == 27
           and stored.get("line_end") == 27 and stored.get("corrected_from") == [2, 4],
           {"result": corrected, "evidence": stored})
    expect(checks, "the filed issue cites the quote's actual line, not the reviewer's three-line span",
           "## Evidence\n`tests/test_cli.py:27` (lines corrected from 1-3)" in relocated.get("body", "")
           and scout_replay.MISCOUNTED.evidence[0].quote in relocated.get("body", ""), relocated)
    ambiguous = second_seen.get(scout_replay.AMBIGUOUS.title, {})
    expect(checks, "two out-of-window matches are dropped without a nearby match",
           ambiguous.get("action") == "dropped" and "does not contain the quoted text" in ambiguous.get("reason", "")
           and not filed(state, scout_replay.AMBIGUOUS.title), ambiguous)
    expect(checks, "auto: a bug with a track record and a failing repro is filed ready, saying why",
           malformed.get("labels") == ["cardinal:ready"] and "Filed ready by Cardinal's scout: 4 decided bug" in
           malformed.get("body", "") and "## Suggested test" in malformed.get("body", ""), malformed)
    expect(checks, "auto: a feature below its bars stays proposed",
           count.get("labels") == ["cardinal:proposed"] and "not auto: 1 decided feature" in
           second_seen.get(scout_replay.COUNT.title, {}).get("reason", ""), second_seen.get(scout_replay.COUNT.title))
    expect(checks, "repro worktrees are always removed",
           not [path.name for path in (product.home / "worktrees").rglob("scout-*")], list((product.home / "worktrees").rglob("*"))[:5])
    expect(checks, "the held product decision is filed for a person, not as work",
           decision.get("labels") == ["cardinal:needs-human"] and "product decision" in decision.get("body", ""), decision)

    state["issues"][str(approved.get("number"))].update(labels=["cardinal:done"], state="CLOSED")
    json_file(state_path, state)
    before = len(recorded_runs(product.home))
    code, third = product("scout", "--once", "--repo", SLUG)
    third_runs = recorded_runs(product.home)[before:]
    check_test_runs(checks, [("pass 1", first_runs, [scout_replay.MISSING_INPUT.title]),
                         ("pass 2", second_runs, [scout_replay.MALFORMED.title, scout_replay.UNKNOWN.title]),
                         ("pass 3", third_runs, [])])
    code_status, status = product("scout", "status", "--repo", SLUG)
    categories = (status or {}).get("categories", {}) if isinstance(status, dict) else {}
    expect(checks, "pass 3 surveys nothing new and reads the approved issue's landing as done",
           code == 0 and isinstance(third, dict) and third.get("areas") == [] and
           [item.get("outcome") for item in third.get("outcomes", [])] == ["done"], third)
    expect(checks, "scout status reports the track record per category",
           code_status == 0 and categories.get("bug", {}).get("done") == 4 and categories.get("bug", {}).get("gate") == "auto"
           and categories.get("feature", {}).get("rejected") == 1 and categories.get("feature", {}).get("gate") == "proposed",
           status)

    serve_checks(product, bare, checks, artifact, (third or {}).get("pass_id") if isinstance(third, dict) else None)

    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations, "test_runs": recorded_runs(product.home)}


def cli(product: Product, *args: str, timeout: int = 60):
    command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home), *args]
    return run_bounded(command, timeout, env=product.env)


def next_pass_times(product: Product) -> list[str]:
    result = cli(product, "logs", "query")
    records = [json.loads(line)["record"] for line in result.stdout.splitlines() if line.strip()]
    return [record["data"]["at"] for record in records if record.get("event") == "scout_next_pass"]


def serve_checks(product: Product, bare: Path, checks: dict, artifact: Path, previous: str | None) -> None:
    """Daily schedule: bad `at` and `at` with --interval are refused; serve mode logs the next pass, runs
    one at that time, and stops on SIGTERM."""
    write(product.home, bare, {**SCOUT, "at": "25:00"})
    refused = cli(product, "scout", "--once", "--repo", SLUG)
    expect(checks, "an `at` outside 00:00-23:59 is refused when the config loads",
           refused.returncode != 0 and "at" in refused.stderr, refused.stderr[-500:])

    target = (datetime.now() + timedelta(minutes=2)).replace(second=0, microsecond=0)
    write(product.home, bare, {**SCOUT, "at": target.strftime("%H:%M")})
    both = cli(product, "scout", "--repo", SLUG, "--interval", "60")
    expect(checks, "`at` together with --interval is refused, naming both",
           both.returncode != 0 and "at" in both.stderr and "--interval" in both.stderr, both.stderr[-500:])

    known = len(next_pass_times(product))
    command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(product.home), "-v", "scout", "--repo", SLUG]
    with (artifact / "scout-serve.log").open("w") as output:
        process = subprocess.Popen(command, env=product.env, start_new_session=True, stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 300
            scheduled: list[str] = []
            while time.monotonic() < deadline and process.poll() is None and not scheduled:
                scheduled = next_pass_times(product)[known:]
                time.sleep(0.5)
            logged = datetime.fromisoformat(scheduled[0]) if scheduled else None
            expect(checks, "serve mode logs scout_next_pass with the next host-local HH:MM before sleeping",
                   logged is not None and logged.replace(tzinfo=None) == target, scheduled)
            finished: dict = {}
            while time.monotonic() < deadline and process.poll() is None:
                code, status = product("scout", "status", "--repo", SLUG)
                last = (status or {}).get("last_pass") if isinstance(status, dict) else None
                if last and last["pass_id"] != previous and last.get("ended_at"):
                    finished = last
                    break
                time.sleep(2)
            expect(checks, "one pass runs at that time and completes", bool(finished) and not finished.get("failure_kind"),
                   finished)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    expect(checks, "the serve process stops on SIGTERM", process.poll() == 128 + signal.SIGTERM,
           process.returncode)
