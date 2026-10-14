"""`python -m cardinal_harness`: grade the Cardinal product from outside.

  offline --scenario NAME [--repeat N] single | daemon | ci-failure | ci-repair | deploy | cleaner | monitor | base-sync | post-merge | parallel | daemon-restart | worker-killed | scout | documentation | claude-code
  live --suite PATH --model SPEC       the product's daemon against the live GitHub test repository
  propagate --model SPEC              an app error in the live test repo becomes an issue Cardinal fixes
  live-scout --model SPEC [--keep-issues]  one scout pass on a seeded branch of the live test repository
  live-scout --cleanup RUN             close a kept live-scout run's issues and delete its seed branch
  self-seed [--reseed]                 snapshot Cardinal into the self-test repository and file its issues
  clean --manifest PATH [--model SPEC] restore the live test repository from a pinned manifest
Every command writes artifacts/e2e/<run-id>/report.json with a rerun command, and exits nonzero
unless every check passed.
"""

import argparse
import itertools
import json
import os
import shlex
import socket
import sys
import time
import uuid
from pathlib import Path

from cardinal_harness.product import ROOT, json_file


def repeated(scenario: str, times: int, artifact: Path) -> dict:
    """Run one offline scenario `times` times in a row, each in its own run-NN/ artifact, and stop at the
    first failure. The report records the host and how many consecutive runs passed, so a reliability
    claim such as "10 out of 10 in a row" is backed by one artifact rather than by someone's say-so."""
    from cardinal_harness import offline as scenarios
    runs: list[dict] = []
    checks: dict = {}
    for number in range(1, times + 1):
        target = artifact / f"run-{number:02d}"
        target.mkdir(parents=True)
        started = time.monotonic()
        report = scenarios.run(scenario, target)
        json_file(target / "report.json", report)
        failed = sorted(name for name, check in report["checks"].items() if not check["ok"])
        entry = {"run": number, "passed": report["passed"], "seconds": round(time.monotonic() - started, 1),
                 "artifact": str(target), "failed_checks": failed}
        runs.append(entry)
        checks[f"run {number} of {times}: every check passed"] = {"ok": report["passed"], "detail": entry}
        if not report["passed"]:
            break
    consecutive = sum(1 for _ in itertools.takewhile(lambda entry: entry["passed"], runs))
    return {"scenario": scenario, "host": socket.gethostname(), "repeat": times, "attempted": len(runs),
            "consecutive_passes": consecutive, "runs": runs, "checks": checks,
            "passed": consecutive == times}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="cardinal_harness")
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("--scenario", choices=["single", "daemon", "multi-daemon", "repos-check", "ci-failure", "ci-repair", "deploy", "cleaner", "monitor", "base-sync", "post-merge", "parallel", "daemon-restart", "worker-killed", "scout", "documentation", "file-tool-refusal", "gh-retries", "gh-server-errors", "push-retries", "setup-recovery", "claude-code"], required=True)
    offline.add_argument("--repeat", type=int, default=1, metavar="N",
                         help="run the scenario N times in a row, stopping at the first failure, and report "
                              "how many consecutive runs passed on this host")
    live = sub.add_parser("live")
    live.add_argument("--suite", type=Path, default=ROOT / "tests" / "fixtures" / "github_notes_suite.json")
    live.add_argument("--model", required=True)
    live.add_argument("--only", nargs="*", help="case keys to run, in suite order")
    live_scout = sub.add_parser("live-scout")
    live_scout.add_argument("--model")
    live_scout.add_argument("--keep-issues", action="store_true", help="leave the issues and seed branch for a person")
    live_scout.add_argument("--cleanup", metavar="RUN", help="clean up a run kept with --keep-issues")
    propagate = sub.add_parser("propagate")
    propagate.add_argument("--model", required=True)
    self_seed = sub.add_parser("self-seed")
    self_seed.add_argument("--reseed", action="store_true", help="replace an already seeded main, under a lease")
    clean = sub.add_parser("clean")
    clean.add_argument("--manifest", type=Path, required=True)
    clean.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.command == "offline" and args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.command == "live-scout" and not (args.model or args.cleanup):
        parser.error("live-scout needs --model, or --cleanup RUN")

    artifact = ROOT / "artifacts" / "e2e" / uuid.uuid4().hex
    artifact.mkdir(parents=True)
    rerun = ["uv", "run", "--locked", "--extra", "openai", "--extra", "e2e", "python", "-m", "cardinal_harness", *argv]
    if os.environ.get("UV_CACHE_DIR"):
        rerun = ["env", f"UV_CACHE_DIR={os.environ['UV_CACHE_DIR']}", *rerun]
    if args.command == "offline" and args.repeat > 1:
        report = repeated(args.scenario, args.repeat, artifact)
    elif args.command == "offline":
        from cardinal_harness import offline as scenarios
        report = scenarios.run(args.scenario, artifact)
    elif args.command == "live":
        from cardinal_harness import live as suite
        report = suite.run_suite(args.suite, args.model, artifact, args.only)
    elif args.command == "live-scout":
        from cardinal_harness import live_scout
        report = live_scout.cleanup_later(args.cleanup) if args.cleanup else \
            live_scout.run(args.model, artifact, args.keep_issues)
    elif args.command == "self-seed":
        from cardinal_harness import self_seed
        try:
            report = self_seed.seed(args.reseed, artifact)
        except Exception as exc:  # noqa: BLE001 - recorded in the report, which the exit code reflects
            report = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
    elif args.command == "propagate":
        from cardinal_harness import live_monitor
        report = live_monitor.run_exclusive(args.model, artifact)
    else:
        from cardinal_harness import live as suite
        report = suite.clean_from_manifest(args.manifest, args.model, artifact)
    report["rerun_command"] = shlex.join(rerun)
    json_file(artifact / "report.json", report)
    print(json.dumps({"artifact": str(artifact), "passed": report["passed"]}))
    return 0 if report["passed"] else 1
