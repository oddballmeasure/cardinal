"""`cardinal` command line."""

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from cardinal.cli import commands


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cardinal", description="Resolve GitHub issues with a team of coding agents.")
    root.add_argument("--home", type=Path, help="Cardinal home (default $CARDINAL_HOME or ~/.cardinal)")
    root.add_argument("-v", "--verbose", action="count", default=0)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Write a starter cardinal.toml")
    init.add_argument("--repo", required=True, help="owner/name of the first target repository")

    repos = sub.add_parser("repos", help="Inspect configured repositories")
    repos_sub = repos.add_subparsers(dest="repos_command", required=True)
    check = repos_sub.add_parser("check", help="Verify credentials, remote, labels and test command")
    check.add_argument("--repo")

    run = sub.add_parser("run", help="Run one issue now, in the foreground")
    run.add_argument("issue", type=int)
    run.add_argument("--repo")

    status = sub.add_parser("status", help="Show runs")
    status.add_argument("issue", type=int, nargs="?")
    status.add_argument("--repo")
    status.add_argument("--json", action="store_true")

    resume = sub.add_parser("resume", help="Answer a run that is waiting for a person")
    resume.add_argument("issue", type=int)
    resume.add_argument("--repo")
    answer = resume.add_mutually_exclusive_group(required=True)
    answer.add_argument("--approve", action="store_true")
    answer.add_argument("--reject", action="store_true")
    resume.add_argument("--note", default="")

    daemon = sub.add_parser("daemon", help="Work ready issues for one repository")
    daemon.add_argument("--repo")
    daemon.add_argument("--once", action="store_true", help="Drain the ready queue, then exit")
    daemon.add_argument("--interval", type=int, default=60)

    logs = sub.add_parser("logs", help="Log record contract")
    logs_sub = logs.add_subparsers(dest="logs_command", required=True)
    logs_sub.add_parser("schema", help="Print the LogRecord JSON Schema other repositories write against")

    sub.add_parser("ingest", help="Accept LogRecords from running apps over HTTP")

    watch = sub.add_parser("monitor", help="File issues for repeated errors in the logs")
    watch.add_argument("--once", action="store_true", help="Scan once, then exit")
    watch.add_argument("--interval", type=int, default=300)
    return root


def terminate(signum, frame):  # noqa: ANN001, ARG001
    """SIGTERM unwinds like Ctrl-C, so claims are released and test process groups are killed."""
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, terminate)
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING - 10 * min(args.verbose, 2),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    try:
        result = commands.dispatch(args)
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    if isinstance(result, int):
        return result
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
