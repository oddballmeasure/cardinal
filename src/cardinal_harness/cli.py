"""`python -m cardinal_harness`: grade the Cardinal product from outside.

  offline --scenario NAME              single | daemon | ci-failure | deploy | cleaner | monitor, all without network
  live --suite PATH --model SPEC       the product's daemon against the live GitHub test repository
  clean --manifest PATH [--model SPEC] restore the live test repository from a pinned manifest
Every command writes artifacts/e2e/<run-id>/report.json with a rerun command, and exits nonzero
unless every check passed.
"""

import argparse
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

from cardinal_harness.product import ROOT, json_file


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="cardinal_harness")
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("--scenario", choices=["single", "daemon", "ci-failure", "deploy", "cleaner", "monitor"], required=True)
    live = sub.add_parser("live")
    live.add_argument("--suite", type=Path, default=ROOT / "tests" / "fixtures" / "github_notes_suite.json")
    live.add_argument("--model", required=True)
    live.add_argument("--only", nargs="*", help="case keys to run, in suite order")
    clean = sub.add_parser("clean")
    clean.add_argument("--manifest", type=Path, required=True)
    clean.add_argument("--model", required=True)
    args = parser.parse_args(argv)

    artifact = ROOT / "artifacts" / "e2e" / uuid.uuid4().hex
    artifact.mkdir(parents=True)
    rerun = ["uv", "run", "--locked", "--extra", "openai", "--extra", "e2e", "python", "-m", "cardinal_harness", *argv]
    if os.environ.get("UV_CACHE_DIR"):
        rerun = ["env", f"UV_CACHE_DIR={os.environ['UV_CACHE_DIR']}", *rerun]
    if args.command == "offline":
        from cardinal_harness import offline as scenarios
        report = scenarios.run(args.scenario, artifact)
    elif args.command == "live":
        from cardinal_harness import live as suite
        report = suite.run_suite(args.suite, args.model, artifact, args.only)
    else:
        from cardinal_harness import live as suite
        report = suite.clean_from_manifest(args.manifest, args.model, artifact)
    report["rerun_command"] = shlex.join(rerun)
    json_file(artifact / "report.json", report)
    print(json.dumps({"artifact": str(artifact), "passed": report["passed"]}))
    return 0 if report["passed"] else 1
