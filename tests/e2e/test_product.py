"""End-to-end: the `cardinal` product, driven only through its CLI by the grading harness.

Each test runs one harness scenario as a subprocess and reads back the artifact it leaves in
artifacts/e2e/<run-id>/. A scenario passes only if every check it makes from outside the
product passed: fake-GitHub state, the bare remote's refs, `cardinal status --json`, and the
independent acceptance tests. Rerun any scenario with the report's `rerun_command`.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def harness(*args: str, timeout: int) -> dict:
    process = subprocess.run([sys.executable, "-m", "cardinal_harness", *args], cwd=ROOT, text=True,
                             capture_output=True, check=False, timeout=timeout)
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
    harness("offline", "--scenario", "ci-failure", timeout=900)


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


@pytest.mark.skipif(os.environ.get("CARDINAL_LIVE") != "1", reason="set CARDINAL_LIVE=1: uses GitHub, Actions and a paid model")
def test_live_github_suite_through_the_daemon():
    model = os.environ.get("CARDINAL_LIVE_MODEL", "openai:gpt-6-sol")
    report = harness("live", "--model", model, timeout=6 * 3600)
    assert len(report["cases"]) == 6 and all(case["passed"] for case in report["cases"])
