"""End-to-end: the `cardinal` product, driven only through its CLI by the grading harness.

Each test runs one harness scenario as a subprocess and reads back the artifact it leaves in
artifacts/e2e/<run-id>/. A scenario passes only if every check it makes from outside the
product passed: fake-GitHub state, the bare remote's refs, `cardinal status --json`, and the
independent acceptance tests. Rerun any scenario with the report's `rerun_command`.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import jsonschema
import pytest

from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]


def harness(*args: str, timeout: int) -> dict:
    process = run_bounded([sys.executable, "-m", "cardinal_harness", *args], timeout, cwd=ROOT)
    assert process.returncode >= 0, f"harness {args} was killed after {timeout}s: {process.stderr[-3000:]}"
    summary = json.loads(process.stdout.strip().splitlines()[-1])
    artifact = Path(summary["artifact"])
    report = json.loads((artifact / "report.json").read_text())
    assert report["rerun_command"], "every run must be repeatable from its artifact"
    failed = {name: check["detail"] for name, check in report.get("checks", {}).items() if not check["ok"]}
    assert not failed, f"{artifact}: failed checks {json.dumps(failed, default=str)[:4000]}"
    assert process.returncode == 0 and report["passed"], f"{artifact}: {process.stderr[-3000:]}"
    return report


def test_two_dependent_tickets_wait_for_ci_merge_deploy_and_pass_acceptance():
    """The lead scenario: one issue, two ordered tickets, CI missing→pending→success, merge,
    a deploy whose health turns ready only on the second poll, then the hidden acceptance tests."""
    report = harness("offline", "--scenario", "single", timeout=900)
    assert len(report["checks"]) == 10


def test_daemon_orders_claims_pauses_for_people_and_labels_failures():
    """Five issues: #12 builds on #11's merge; #13 is declined and #15 approved via `resume`;
    #14 never passes its tests, then is retried over its old branch; a second drain claims nothing."""
    report = harness("offline", "--scenario", "daemon", timeout=1800)
    assert len(report["checks"]) == 17


def test_failed_ci_leaves_pr_open_and_main_untouched():
    """Two required checks, one fails: the failure names only that one."""
    harness("offline", "--scenario", "ci-failure", timeout=900)


def test_base_moving_during_a_run_is_merged_in_resolved_and_verified_again():
    """Other merges land on main while #11 and #12 are worked: a conflict before publishing, a clean
    move, and a conflict GitHub reports while waiting for CI are each merged in, resolved through a
    SYNC ticket where needed, verified again, and merged without waiting out the CI timeout."""
    report = harness("offline", "--scenario", "base-sync", timeout=1200)
    assert len(report["checks"]) == 12


def test_checks_failing_after_merge_are_rerun_once_then_filed_without_looping():
    """A flaky check on a merge commit passes on re-run and files nothing; one that fails twice files
    one ready follow-up, which is fixed; the fix's own failing merge goes to a person."""
    report = harness("offline", "--scenario", "post-merge", timeout=1800)
    assert len(report["checks"]) == 7


def test_parallel_workers_plan_one_at_a_time_and_land_sibling_merges():
    """Three workers: #11, #17 and #18 overlap while #12 waits for #11, which it builds on. Planning
    holds the intake lock one run at a time; #17 and #18 edit the same file, so the later merge
    brings in the earlier one; each run records its own PR's merge commit."""
    report = harness("offline", "--scenario", "parallel", timeout=1500)
    assert len(report["checks"]) == 8


def test_a_killed_worker_is_settled_by_the_next_poll_and_reruns():
    report = harness("offline", "--scenario", "worker-killed", timeout=900)
    assert len(report["checks"]) == 4


def test_ci_failure_log_goes_back_to_the_coder_and_the_repaired_head_merges():
    """CI fails on the first pushed head; the coder gets the job log, the verifier rechecks, and the
    same PR merges the repaired head once CI passes on it."""
    harness("offline", "--scenario", "ci-repair", timeout=900)


def test_deploy_layouts_and_failures_after_a_real_merge():
    """A root-level deploy pair deploys; a failing script, health that never matches, and an
    ambiguous layout each fail the run after the merge without recording a deployed revision."""
    harness("offline", "--scenario", "deploy", timeout=900)


def test_cleaner_restores_remote_and_refuses_a_stale_head():
    harness("offline", "--scenario", "cleaner", timeout=600)


def test_monitor_files_triaged_issues_from_cardinal_crashes_and_app_records():
    """A crash with unwritable log files still settles; app records arrive through ingest; the
    monitor files each defect on its own repository, caps a pass, never duplicates an open
    finding, re-files a recurrence, and every JSONL line matches the published schema."""
    report = harness("offline", "--scenario", "monitor", timeout=900)
    assert len(report["checks"]) == 15


def test_scout_proposes_checked_work_and_learns_from_outcomes():
    """Two areas surveyed, then the third: a real defect is filed as proposed with its evidence and a
    failing repro test; a fabricated quote, a duplicate, a reviewer refusal and an unreproduced bug
    are dropped; the cap holds the overflow; the daemon leaves proposals alone; approval and rejection
    are read back and the rejection reaches the next survey; in auto mode a bug with a track record
    goes ready while a feature below its bars stays proposed; a held product decision goes to a person."""
    report = harness("offline", "--scenario", "scout", timeout=900)
    assert len(report["checks"]) == 18


def logs_cli(home: Path, *args: str):
    return run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), "logs", *args],
                       timeout=30, cwd=ROOT)


def test_logs_query_streams_stored_records_with_composable_filters(tmp_path):
    home = tmp_path / "cardinal-home"
    init = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home),
                        "init", "--repo", "team/app"], timeout=30, cwd=ROOT)
    assert init.returncode == 0, init.stderr
    schema = logs_cli(home, "schema")
    assert schema.returncode == 0, schema.stderr
    contract = json.loads(schema.stdout)

    # Timestamp order differs from store order; the deleted row leaves a sequence gap.
    specs = [("debug", "team/app", "f1"), ("info", "team/other", "f1"),
             ("warning", "team/app", "f2"), ("error", "team/app", "f1"),
             ("critical", "team/other", "f2"), ("error", "team/app", "f2"),
             ("warning", "team/app", "f1"), ("critical", "team/app", "f1")]
    records = {}
    with sqlite3.connect(home / "store.db") as db:
        for seq, (level, repo, fingerprint) in enumerate(specs, 1):
            record = {"schema_version": 1, "id": f"record-{seq}",
                      "at": f"2025-01-{9 - seq:02d}T00:00:00Z", "level": level,
                      "event": "test_event", "message": f"message {seq}",
                      "source": {"repo": repo, "component": "api"}, "fingerprint": fingerprint}
            jsonschema.validate(record, contract)
            records[seq] = record
            db.execute("INSERT INTO logs (id, at, level, repo, component, event, fingerprint, record) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (record["id"], record["at"], level, repo, "api", record["event"],
                        fingerprint, json.dumps(record)))
        db.execute("DELETE FROM logs WHERE seq = 4")

    def check(expected, *flags):
        result = logs_cli(home, "query", *flags)
        assert result.returncode == 0 and not result.stderr, result.stderr
        lines = result.stdout.splitlines()
        assert len(lines) == len(expected)
        payloads = [json.loads(line) for line in lines]
        assert payloads == [{"seq": seq, "record": records[seq]} for seq in expected]
        for payload in payloads:
            assert type(payload["seq"]) is int
            jsonschema.validate(payload["record"], contract)

    check([1, 2, 3, 5, 6, 7, 8])
    check([3, 5, 6, 7, 8], "--level", "warning")
    check([5, 8], "--level", "critical")
    check([1, 3, 6, 7, 8], "--repo", "team/app")
    check([1, 2, 7, 8], "--fingerprint", "f1")
    check([7, 8], "--level", "warning", "--repo", "team/app", "--fingerprint", "f1")
    check([5, 6], "--after-seq", "3", "--limit", "2")
    check([6, 7], "--level", "warning", "--repo", "team/app",
          "--after-seq", "3", "--limit", "2")
    check([], "--repo", "missing/repo")
    check([], "--limit", "0")

    invalid = logs_cli(home, "query", "--level", "trace")
    assert invalid.returncode == 2
    assert invalid.stdout == ""
    assert "trace" in json.loads(invalid.stderr)["error"]


@pytest.mark.skipif(os.environ.get("CARDINAL_LIVE") != "1", reason="set CARDINAL_LIVE=1: uses GitHub, Actions and a paid model")
def test_live_github_suite_through_the_daemon():
    model = os.environ.get("CARDINAL_LIVE_MODEL", "openai:gpt-6-sol")
    report = harness("live", "--model", model, timeout=6 * 3600)
    assert len(report["cases"]) == 6 and all(case["passed"] for case in report["cases"])
