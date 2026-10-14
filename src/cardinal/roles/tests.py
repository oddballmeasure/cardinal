"""The repository's own test command: the only test oracle Cardinal's agents or runtime use."""

import logging
import os
import re
import signal
import subprocess
from pathlib import Path

from cardinal.contracts.evidence import CommandEvidence

TAIL = 6000
log = logging.getLogger(__name__)


def tail(text: str) -> str:
    return text if len(text) <= TAIL else "…[earlier output clipped]\n" + text[-TAIL:]


def run_repo_tests(worktree: Path, command: list[str], timeout: int) -> CommandEvidence:
    # Own process group: test suites start servers and containers that must die with a timeout.
    process = subprocess.Popen(command, cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env={**os.environ, "CI": "1"})
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return CommandEvidence(command=command, exit_code=process.returncode,
                               stdout_tail=tail(stdout), stderr_tail=tail(stderr))
    except BaseException as exc:
        if not isinstance(exc, subprocess.TimeoutExpired):  # Cardinal is stopping: take the suite with it
            os.killpg(process.pid, signal.SIGKILL)
            raise
        log.warning("test command timed out after %ss", timeout,
                    extra={"event": "test_timeout", "data": {"command": command}})
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return CommandEvidence(command=command, exit_code=124, timed_out=True,
                               stdout_tail=tail(stdout or ""), stderr_tail=tail((stderr or "") + f"\nkilled after {timeout}s"))


def focused_command(command: list[str], test_path: str, test_name: str) -> list[str]:
    """Fill a focused_test_command's {test_path} and {test_name} in each argv element."""
    return [argument.replace("{test_path}", test_path).replace("{test_name}", test_name) for argument in command]


# pytest exits 5 when it collects nothing; these lines mean the same from it or other runners.
NO_TESTS = re.compile(r"no tests ran|collected 0 items|no tests collected|error: not found:|no tests found", re.IGNORECASE)


def ran_no_tests(evidence: CommandEvidence) -> bool:
    return evidence.exit_code == 5 or bool(NO_TESTS.search(evidence.stdout_tail + "\n" + evidence.stderr_tail))


def summary(evidence: CommandEvidence) -> str:
    state = "PASSED" if evidence.passed else ("TIMED OUT" if evidence.timed_out else f"FAILED (exit {evidence.exit_code})")
    return f"{state}: {' '.join(evidence.command)}\n--- stdout ---\n{evidence.stdout_tail}\n--- stderr ---\n{evidence.stderr_tail}"
