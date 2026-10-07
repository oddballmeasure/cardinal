"""Work ready issues in parallel, each as its own `cardinal run N` process, up to max_parallel_runs.

A process per run, not a thread: each has its own store connection, LangGraph thread pools and
SIGTERM unwinding, and the claim, the log sink and test process groups already work across
processes. What the runs share is serialised with file locks (cardinal/locks.py): planning (profile
and intake) runs one issue at a time; coding, verifying, CI waits and syncs overlap; merges take turns.

An issue whose body says it depends on, builds on, requires or is blocked by another issue that is
still running or queued waits for it, so it is planned against that issue's merge.
"""

import json
import logging
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

from cardinal import app
from cardinal.config.models import Repo
from cardinal.daemon import post_merge
from cardinal.github import issues
from cardinal.home import Home
from cardinal.store import inputs

log = logging.getLogger(__name__)

TICK = 10  # seconds between checks on running workers and the ready queue
JUDGE_EVERY = 60  # post-merge judging while slots stay busy; always again after a run settles
STOP_GRACE = 60  # seconds a worker gets to settle after SIGTERM before it is killed
DEPENDS = re.compile(r"\b(?:depends on|builds on|blocked by|requires)\s+#(\d+)", re.IGNORECASE)


class Worker:
    def __init__(self, home: Home, repo: Repo, number: int, verbose: int) -> None:
        self.number = number
        self.output = tempfile.TemporaryFile(mode="w+")
        command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home.root), *["-v"] * verbose,
                   "run", str(number), "--repo", repo.slug]
        # Its own session: Ctrl-C on the daemon reaches workers once, through stop(), not twice.
        self.process = subprocess.Popen(command, stdout=self.output, text=True, start_new_session=True)
        log.info("started worker %s for issue #%s", self.process.pid, number,
                 extra={"event": "worker_started", "data": {"issue": number, "pid": self.process.pid}})

    def result(self) -> dict | None:
        """The settled run `cardinal run` printed, or None when it died without printing one."""
        self.output.seek(0)
        text = self.output.read()
        self.output.close()
        if self.process.returncode == 2:  # refused before starting: claimed elsewhere, or closed
            return {"issue": self.number, "status": "skipped", "reason": "cardinal run refused the issue"}
        try:
            result = json.loads(text)
        except ValueError:
            return None
        return result if isinstance(result, dict) and "status" in result else None


def parallel(home: Home, repo: Repo, db: sqlite3.Connection, verbose: int) -> list[dict]:
    running: dict[int, Worker] = {}
    results: list[dict] = []
    attempted: set[int] = set()
    bodies: dict[int, str] = {}
    judged = float("-inf")
    try:
        while True:
            silent = []
            for number, worker in list(running.items()):
                if worker.process.poll() is None:
                    continue
                del running[number]
                judged = float("-inf")  # it may have merged: judge before the next claim
                result = worker.result()
                if result is None:
                    silent.append(number)
                else:
                    results.append(result)
                    log.info("issue #%s settled %s", number, result["status"])
            results += reap(repo, db, silent)
            pending: list[int] = []
            if len(running) < repo.max_parallel_runs:
                if time.monotonic() - judged >= JUDGE_EVERY:
                    post_merge.judge_all(repo, db)
                    judged = time.monotonic()
                pending = [number for number in issues.ready(repo.slug, repo.labels.ready) if number not in attempted]
                for number in pending:
                    if len(running) >= repo.max_parallel_runs:
                        break
                    blocker = waits_on(repo, number, (set(running) | set(pending)) - {number}, bodies)
                    # With nothing running, a blocker can never settle (a dependency cycle): start anyway.
                    if blocker is not None and running:
                        continue
                    attempted.add(number)
                    log.info("claiming issue #%s", number)
                    running[number] = Worker(home, repo, number, verbose)
            if not running and not [number for number in pending if number not in attempted]:
                return results
            time.sleep(TICK)
    finally:
        stop(running)


def waits_on(repo: Repo, number: int, active: set[int], bodies: dict[int, str]) -> int | None:
    if not active:
        return None
    if number not in bodies:
        issue, _, _ = issues.view(repo.slug, number)
        bodies[number] = issue.body or ""
    named = {int(found) for found in DEPENDS.findall(bodies[number])}
    blocker = min(named & active, default=None)
    if blocker is not None:
        log.info("issue #%s waits for #%s", number, blocker,
                 extra={"event": "issue_waiting", "data": {"issue": number, "blocker": blocker}})
    return blocker


def reap(repo: Repo, db: sqlite3.Connection, silent: list[int]) -> list[dict]:
    """Settle runs whose process died holding its claim; report workers that died before claiming."""
    results = [app.abandon(repo, db, claim) for claim in inputs.orphaned_claims(db, repo.slug)]
    settled = {result["issue"] for result in results}
    for number in silent:
        if number not in settled:
            results.append({"issue": number, "status": "failed", "failure": {
                "kind": "crash", "stage": "worker", "detail": "The worker exited without a result"}})
    return results


def stop(running: dict[int, Worker]) -> None:
    """Stopping the daemon stops its workers; each settles its run as interrupted, like a foreground run."""
    for worker in running.values():
        if worker.process.poll() is None:
            worker.process.terminate()
    deadline = time.monotonic() + STOP_GRACE
    for worker in running.values():
        try:
            worker.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.error("worker %s for issue #%s did not stop; killing it", worker.process.pid, worker.number,
                      extra={"event": "worker_killed", "data": {"issue": worker.number}})
            worker.process.kill()
            worker.process.wait()
