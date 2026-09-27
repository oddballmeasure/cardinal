"""The repository's own test command: the only test oracle Cardinal's agents or runtime use."""

import os
import signal
import subprocess
from pathlib import Path

from cardinal.contracts.evidence import CommandEvidence

TAIL = 6000


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
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return CommandEvidence(command=command, exit_code=124, timed_out=True,
                               stdout_tail=tail(stdout or ""), stderr_tail=tail((stderr or "") + f"\nkilled after {timeout}s"))


def summary(evidence: CommandEvidence) -> str:
    state = "PASSED" if evidence.passed else ("TIMED OUT" if evidence.timed_out else f"FAILED (exit {evidence.exit_code})")
    return f"{state}: {' '.join(evidence.command)}\n--- stdout ---\n{evidence.stdout_tail}\n--- stderr ---\n{evidence.stderr_tail}"
