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
    assert process.stdout.strip(), f"harness {args} produced no report: {process.stderr[-3000:]}"
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
    assert len(report["checks"]) == 11


def test_coder_recovers_from_file_tool_refusals_and_completes_cli_issue():
    report = harness("offline", "--scenario", "file-tool-refusal", timeout=900)
    assert len(report["checks"]) == 17


def test_daemon_orders_claims_pauses_for_people_and_labels_failures():
    """Five issues: #12 builds on #11's merge; #13 is declined and #15 approved via `resume`;
    #14 never passes its tests, then is retried over its old branch; a second drain claims nothing."""
    report = harness("offline", "--scenario", "daemon", timeout=1800)
    assert len(report["checks"]) == 17


def test_unpinned_daemon_drains_two_repositories_and_pinned_daemon_isolates_one():
    report = harness("offline", "--scenario", "multi-daemon", timeout=1200)
    assert len(report["checks"]) == 6


def test_daemon_recovers_partly_applied_setup_edits_and_settles_failed_setup():
    report = harness("offline", "--scenario", "setup-recovery", timeout=900)
    assert len(report["checks"]) == 5


def test_gh_transient_failures_retry_but_permanent_failures_do_not():
    report = harness("offline", "--scenario", "gh-retries", timeout=900)
    assert len(report["checks"]) == 7


def test_github_http_500_in_an_agent_tool_is_retried_then_fails_as_github():
    report = harness("offline", "--scenario", "gh-server-errors", timeout=900)
    assert len(report["checks"]) == 2


def test_verified_branch_push_recovers_from_transient_remote_failure_without_retrying_permanent_rejections():
    report = harness("offline", "--scenario", "push-retries", timeout=1200)
    assert len(report["checks"]) == 11


def test_repos_check_reports_every_repository_and_fails_on_any_probe():
    report = harness("offline", "--scenario", "repos-check", timeout=120)
    assert len(report["checks"]) == 3


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


def test_documentation_only_merges_without_tests_but_code_and_unmet_requirements_do_not():
    """The CLI merges a README-only fix with no test edits or invented boundary coverage;
    unrelated code edits and a README edit missing its requirement cannot be approved."""
    report = harness("offline", "--scenario", "documentation", timeout=900)
    assert len(report["checks"]) == 10


def test_claude_code_engine_runs_an_issue_end_to_end_against_a_fake_claude_cli():
    """Every role is `claude-code:` and CARDINAL_MODEL_PROVIDER is unset, so the real engine and SDK run:
    the fake `claude` CLI answers the stream-json control protocol and drives each stage's tools through
    the SDK's in-process MCP server. The README issue merges, closes and deploys. A session reporting the
    five-hour window above [subscription] max_five_hour_utilization waits for its reset; with no
    [subscription] the same report runs straight through."""
    # Two retries each wait out runner.subscription's +60s.
    report = harness("offline", "--scenario", "claude-code", timeout=2400)
    assert len(report["checks"]) == 23
    assert {"ceiling: a usage_ceiling record carries utilization, ceiling and the window's resetsAt",
            "no ceiling: the session reporting 60% ran straight through with no wait",
            "ceiling: repos check shows the probe's window usage with the ceiling",
            "no-ceiling: repos check shows the probe's window usage and no ceiling"} <= set(report["checks"])


def test_subscription_ceiling_is_validated_when_the_config_loads(tmp_path):
    """[subscription] max_five_hour_utilization must be 0 < x <= 1 and the section refuses other keys;
    a config without the section loads as before."""
    home = tmp_path / "cardinal-home"
    base = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home)]
    init = run_bounded([*base, "init", "--repo", "team/app"], timeout=30, cwd=ROOT)
    assert init.returncode == 0, init.stderr
    config = home / "cardinal.toml"
    template = config.read_text()

    def status(section: str):
        config.write_text(template + section)
        return run_bounded([*base, "status", "--json"], timeout=30, cwd=ROOT)

    for section in ("", "\n[subscription]\nmax_five_hour_utilization = 0.5\n",
                    "\n[subscription]\nmax_five_hour_utilization = 1\n"):
        shown = status(section)
        assert shown.returncode == 0 and json.loads(shown.stdout) == [], (section, shown.stderr)
    for section in ("\n[subscription]\nmax_five_hour_utilization = 0\n",
                    "\n[subscription]\nmax_five_hour_utilization = -0.2\n",
                    "\n[subscription]\nmax_five_hour_utilization = 1.5\n",
                    "\n[subscription]\nmax_five_hour_utilization = 0.5\nmax_weekly_utilization = 0.5\n",
                    "\n[subscription]\n"):
        refused = status(section)
        assert refused.returncode == 2 and refused.stdout == "", (section, refused.stdout)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert "subscription" in error, (section, error)


def test_focused_test_command_is_validated_when_the_config_loads(tmp_path):
    """[[repos]] focused_test_command is optional; when set it needs {test_path} or {test_name} and
    refuses any other placeholder. The template's commented example stays commented."""
    home = tmp_path / "cardinal-home"
    base = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home)]
    init = run_bounded([*base, "init", "--repo", "team/app"], timeout=30, cwd=ROOT)
    assert init.returncode == 0, init.stderr
    config = home / "cardinal.toml"
    template = config.read_text()
    assert '# focused_test_command = ["python", "-m", "pytest", "-q", "{test_path}::{test_name}"]' in template
    assert template.rstrip().endswith("base_sync_rounds with it")  # appended keys land in [[repos]]

    def status(line: str):
        config.write_text(template + line)
        return run_bounded([*base, "status", "--json"], timeout=30, cwd=ROOT)

    for line in ("", 'focused_test_command = ["python", "-m", "pytest", "-q", "{test_path}::{test_name}"]\n',
                 'focused_test_command = ["pytest", "{test_path}"]\n',
                 'focused_test_command = ["pytest", "-k", "{test_name}"]\n'):
        shown = status(line)
        assert shown.returncode == 0 and json.loads(shown.stdout) == [], (line, shown.stderr)
    for line, reason in (('focused_test_command = ["pytest", "-q", "tests"]\n', "{test_path} or {test_name}"),
                         ('focused_test_command = ["pytest", "{test_path}::{foo}"]\n', "{foo}"),
                         ('focused_test_command = ["pytest", "{test_name}", "--x={}"]\n', "{}"),
                         ("focused_test_command = []\n", "empty"),
                         ('focused_test_command = "pytest {test_path}"\n', "list")):
        refused = status(line)
        assert refused.returncode == 2 and refused.stdout == "", (line, refused.stdout)
        error = json.loads(refused.stderr.strip().splitlines()[-1])["error"]
        assert "focused_test_command" in error and reason in error, (line, error)


def test_processes_opening_a_new_store_at_once_all_succeed(tmp_path):
    """A daemon and the `status` polls watching it may all be the first to open a home's store: each
    switches it to WAL and creates its tables, and none may fail with "database is locked"."""
    import subprocess

    home = tmp_path / "cardinal-home"
    base = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home)]
    init = run_bounded([*base, "init", "--repo", "team/app"], timeout=30, cwd=ROOT)
    assert init.returncode == 0, init.stderr
    for round_number in range(3):
        for name in ("store.db", "store.db-wal", "store.db-shm"):
            (home / name).unlink(missing_ok=True)
        processes = [subprocess.Popen([*base, "status", "--json"], cwd=ROOT, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            assert process.returncode == 0 and json.loads(stdout) == [], (round_number, stderr[-3000:])


def test_store_from_before_usage_columns_is_migrated_on_open_without_losing_rows(tmp_path):
    home = tmp_path / "cardinal-home"
    home.mkdir()
    with sqlite3.connect(home / "store.db") as db:  # agent_calls as it was before model/window/utilization
        db.execute("CREATE TABLE agent_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,"
                   " stage TEXT NOT NULL, ticket_id TEXT, started_at TEXT NOT NULL, seconds REAL NOT NULL,"
                   " input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, tool_calls TEXT NOT NULL,"
                   " outcome TEXT NOT NULL, transcript TEXT NOT NULL)")
        db.execute("INSERT INTO agent_calls (run_id, stage, started_at, seconds, input_tokens, output_tokens,"
                   " tool_calls, outcome, transcript) VALUES ('old', 'coder', '2025-01-01T00:00:00+00:00', 1.5,"
                   " 3, 4, '[]', 'ok', '[]')")
    base = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home)]
    init = run_bounded([*base, "init", "--repo", "team/app"], timeout=30, cwd=ROOT)
    assert init.returncode == 0, init.stderr
    with sqlite3.connect(home / "store.db") as db:
        db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, started_at)"
                   " VALUES ('old', 't', 'team/app', 7, 'done', '2025-01-01T00:00:00+00:00')")
        columns = {row[1] for row in db.execute("PRAGMA table_info(agent_calls)")}
    assert {"model", "window", "utilization"} <= columns
    for _ in range(2):  # opening it again must not fail
        shown = run_bounded([*base, "status", "7", "--json"], timeout=30, cwd=ROOT)
        assert shown.returncode == 0, shown.stderr
        calls = json.loads(shown.stdout)[0]["agent_calls"]
        assert len(calls) == 1 and calls[0]["stage"] == "coder" and calls[0]["input_tokens"] == 3
        assert not {"model", "window", "utilization"} & set(calls[0])


def test_parallel_workers_plan_one_at_a_time_and_land_sibling_merges():
    """Three workers: #11, #17 and #18 overlap while #12 waits for #11, which it builds on. Planning
    holds the intake lock one run at a time; #17 and #18 edit the same file, so the later merge
    brings in the earlier one; each run records its own PR's merge commit."""
    report = harness("offline", "--scenario", "parallel", timeout=1500)
    assert len(report["checks"]) == 8


def test_graceful_daemon_restart_requeues_serial_and_parallel_runs_for_both_signals():
    """Run back to back with `--repeat`, which records the host and the consecutive passes; the same
    command with `--repeat 10` is the issue's 10-in-a-row reliability check."""
    summary = harness("offline", "--scenario", "daemon-restart", "--repeat", "2", timeout=3600)
    assert summary["scenario"] == "daemon-restart" and summary["host"]
    assert summary["repeat"] == summary["attempted"] == summary["consecutive_passes"] == 2
    assert [run["run"] for run in summary["runs"]] == [1, 2]
    assert "--repeat 2" in summary["rerun_command"]
    for run in summary["runs"]:
        assert run["passed"] and not run["failed_checks"], run
        report = json.loads((Path(run["artifact"]) / "report.json").read_text())
        assert report["passed"] and len(report["checks"]) == 15  # four stop cases of three, plus SIGTERM after settling
        for case in ("serial-sigterm", "parallel-sigterm", "serial-sigint", "parallel-sigint"):
            reached = report["checks"][f"{case}: daemon reached an in-flight run"]["detail"]
            assert reached["runs"][-1]["status"] == "running" and reached["runs"][-1]["branch"], reached


def test_daemon_restart_kills_instead_of_signalling_a_daemon_with_no_run_in_flight():
    """When no run is in flight before the deadline, every stop case fails saying the deadline expired,
    the daemon is SIGKILLed rather than sent the graceful signal, and the report keeps its 15 checks.
    A repeated run stops at that first failure and reports zero consecutive passes."""
    import signal

    process = run_bounded([sys.executable, "-m", "cardinal_harness", "offline", "--scenario", "daemon-restart",
                           "--repeat", "3"], 1800,
                          cwd=ROOT, env={**os.environ, "CARDINAL_HARNESS_IN_FLIGHT_SECONDS": "0"})
    assert process.stdout.strip(), process.stderr[-3000:]
    summary = json.loads(process.stdout.strip().splitlines()[-1])
    repeated = json.loads((Path(summary["artifact"]) / "report.json").read_text())
    assert process.returncode == 1 and not summary["passed"] and not repeated["passed"]
    assert repeated["repeat"] == 3 and repeated["attempted"] == 1 and repeated["consecutive_passes"] == 0
    assert [check["ok"] for check in repeated["checks"].values()] == [False]
    report = json.loads((Path(repeated["runs"][0]["artifact"]) / "report.json").read_text())
    assert not report["passed"]
    assert repeated["runs"][0]["failed_checks"] == sorted(name for name, check in report["checks"].items()
                                                         if not check["ok"])
    checks = report["checks"]
    assert len(checks) == 15
    cases = ("serial-sigterm", "parallel-sigterm", "serial-sigint", "parallel-sigint")
    for case in cases:
        reached = checks[f"{case}: daemon reached an in-flight run"]
        assert not reached["ok"] and "deadline of 0s expired" in reached["detail"]["reason"], reached
        assert "elapsed_seconds" in reached["detail"] and reached["detail"]["log"].endswith(f"{case}-daemon.log")
        for dependent in ("shutdown requeued and released its claim", "restart finishes without manual relabel or error"):
            check = checks[f"{case}: {dependent}"]
            assert not check["ok"] and check["detail"]["exit"] == -signal.SIGKILL, check
    assert {name for name, check in checks.items() if not check["ok"]} == {
        name for name in checks if name.split(":")[0] in cases}


def test_a_killed_worker_is_settled_by_the_next_poll_and_reruns():
    report = harness("offline", "--scenario", "worker-killed", timeout=900)
    assert len(report["checks"]) == 5


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
    goes ready while a feature below its bars stays proposed; a held product decision goes to a person.
    Recorded test invocations show the full suite ran once per pass and each repro ran only its focused test."""
    report = harness("offline", "--scenario", "scout", timeout=900)
    assert len(report["checks"]) == 28


def test_live_scout_stops_before_seeding_when_earlier_runs_left_issues_or_branches(tmp_path, monkeypatch):
    """Fake gh over a bare remote holds an open proposed issue, a needs-human one, and a scout-seed branch:
    live-scout reports them with their cleanup commands and neither pushes nor edits anything."""
    import subprocess

    from cardinal_harness import fake_gh, live_scout
    from cardinal_harness.offline import world

    suite = json.loads((ROOT / json.loads(live_scout.FIXTURE.read_text())["suite"]).read_text())
    bare, state_path, state = world(tmp_path, ready=[])
    run_id = "feedc0ffee12"
    subprocess.run(["git", "--git-dir", str(bare), "branch", f"scout-seed-{run_id}", "main"], check=True)
    state["repository"] = suite["repository"]
    state["issues"] = {
        "40": {"number": 40, "title": "returns uses instead of count", "body": "", "state": "OPEN",
               "labels": ["cardinal:proposed"], "comments": []},
        "41": {"number": 41, "title": "needs a person", "body": "", "state": "OPEN",
               "labels": ["cardinal:needs-human"], "comments": []},
        "42": {"number": 42, "title": "closed one", "body": "", "state": "CLOSED",
               "labels": ["cardinal:proposed"], "comments": []},
        "43": {"number": 43, "title": "unrelated", "body": "", "state": "OPEN", "labels": [], "comments": []},
    }
    state_path.write_text(json.dumps(state))
    monkeypatch.setenv("PATH", f"{fake_gh.install(tmp_path / 'bin').parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CARDINAL_FAKE_GH_STATE", str(state_path))
    monkeypatch.setattr(live_scout, "use_github_cli_git_credentials", lambda: None)

    def refs() -> str:
        return subprocess.run(["git", "--git-dir", str(bare), "for-each-ref"], text=True, capture_output=True,
                              check=True).stdout

    refs_before, issues_before = refs(), json.loads(state_path.read_text())["issues"]
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    report = live_scout.run_locked("claude-code:sonnet", artifact, False, remote_url=str(bare))

    assert report["passed"] is False and not report["checks"] and "cleanup" not in report
    leftovers = report["preflight_leftovers"]
    assert sorted(item["number"] for item in leftovers["issues"]) == [40, 41]
    assert leftovers["branches"] == [f"scout-seed-{run_id}"]
    assert live_scout.CLEANUP.format(run_id=run_id) in leftovers["commands"]
    assert any("#40" in command for command in leftovers["commands"])
    after = json.loads(state_path.read_text())
    assert after["issues"] == issues_before and refs() == refs_before
    assert all(call[:2] == ["issue", "list"] for call in after["calls"]), after["calls"]
    assert not (artifact / live_scout.MANIFEST).exists()

    # Without leftovers the preflight finds nothing, and the run would go on to seed.
    subprocess.run(["git", "--git-dir", str(bare), "branch", "-D", f"scout-seed-{run_id}"], check=True,
                   capture_output=True)
    for issue in after["issues"].values():
        issue["state"] = "CLOSED"
    state_path.write_text(json.dumps(after))
    assert live_scout.preflight(suite["repository"], str(bare)) is None


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
