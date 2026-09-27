"""Start, resume and settle runs. The CLI and the daemon both come through here."""

import logging
import sqlite3
import uuid
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from cardinal.config.models import Config, Repo
from cardinal.github import issues
from cardinal.graph.builder import build
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind
from cardinal.home import Home
from cardinal.repo.git import remove_worktree
from cardinal.store import inputs, runs
from cardinal.store.db import now
from cardinal.store.recorder import Recorder

log = logging.getLogger(__name__)


@contextmanager
def checkpointer(home: Home):
    connection = sqlite3.connect(home.checkpoints, check_same_thread=False)
    try:
        yield SqliteSaver(connection)
    finally:
        connection.close()


def start(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, number: int) -> dict:
    """Run one issue from the top. Refuses an issue another run has claimed."""
    issue, _, state = issues.view(repo.slug, number)
    if state != "OPEN":
        raise ValueError(f"Issue #{number} is {state.lower()}")
    run_id = uuid.uuid4().hex[:12]
    if not inputs.claim(db, repo.slug, number, run_id):
        raise ValueError(f"Issue #{number} is already claimed by another run")
    thread_id = f"{repo.slug}#{number}#{run_id}"
    try:
        runs.create(db, run_id, thread_id, repo.slug, number)
        issues.set_state(repo.slug, number, repo.labels, repo.labels.working)
    except Exception:
        inputs.release(db, repo.slug, number)  # a claim nobody is driving would block the issue forever
        raise
    run = Run(home, config, repo, db, Recorder(db, run_id), run_id, issue)
    run.recorder.event("run", "started", {"issue": issue.model_dump(), "thread_id": thread_id})
    return drive(run, thread_id, {"status": "running"})


def resume(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, number: int, action: str, note: str) -> dict:
    row = runs.latest(db, repo.slug, number)
    if row is None or row["status"] != "awaiting_human":
        raise ValueError(f"Issue #{number} has no run waiting for a person")
    if not inputs.claim(db, repo.slug, number, row["run_id"]):
        raise ValueError(f"Issue #{number} is already claimed by another run")
    try:
        issue, _, _ = issues.view(repo.slug, number)
        runs.update(db, row["run_id"], status="running")
        issues.set_state(repo.slug, number, repo.labels, repo.labels.working)
    except Exception:
        inputs.release(db, repo.slug, number)
        raise
    run = Run(home, config, repo, db, Recorder(db, row["run_id"]), row["run_id"], issue)
    return drive(run, row["thread_id"], Command(resume={"action": action, "note": note}))


def drive(run: Run, thread_id: str, payload) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        try:
            with checkpointer(run.home) as saver:
                graph = build(run, saver)
                graph.invoke(payload, config=config)
                snapshot = graph.get_state(config)
            values, paused = snapshot.values, bool(snapshot.next)
        except Exception as exc:  # noqa: BLE001 - the graph itself failed; the run row and labels still settle
            log.exception("run %s crashed outside a node", run.run_id)
            values = {"status": "failed", "failure": {"kind": FailureKind.CRASH.value, "detail": repr(exc)[:2000],
                                                      "stage": "graph"}}
            paused = False
        except (KeyboardInterrupt, SystemExit):
            # Stopped from outside. Record it and put the issue in the error queue, then keep stopping.
            settle(run, {"status": "failed", "failure": {"kind": FailureKind.CRASH.value, "stage": "interrupted",
                                                         "detail": "Cardinal was stopped during this run"}}, False)
            raise
        return settle(run, values, paused)
    finally:
        inputs.release(run.db, run.repo.slug, run.issue.number)


def settle(run: Run, values: dict, paused: bool) -> dict:
    repo, number, labels = run.repo, run.issue.number, run.repo.labels
    status = "awaiting_human" if paused else values.get("status", "failed")
    failure = values.get("failure") or {}
    runs.update(run.db, run.run_id, status=status, failure_kind=failure.get("kind"),
                failure_detail=failure.get("detail"), ended_at=None if paused else now())
    run.recorder.event("run", "settled", {"status": status, "failure": failure})
    if status == "done":
        issues.set_state(repo.slug, number, labels, labels.done)
        remove_worktree(run.clone, run.worktree)
    elif status == "awaiting_human":
        issues.set_state(repo.slug, number, labels, labels.needs_human)
        issues.comment(repo.slug, number, "Cardinal needs a decision before continuing:\n\n> "
                       + values.get("question", "").replace("\n", "\n> ")
                       + "\n\nAnswer with `cardinal resume " + str(number) + " --approve --note \"...\"` or `--reject`.")
    elif status == "rejected":
        issues.set_state(repo.slug, number, labels, labels.needs_human)
        reason = (values.get("decision") or {}).get("reason", "No reason recorded.")
        issues.comment(repo.slug, number, f"Cardinal is not taking this issue: {reason}")
    else:
        issues.set_state(repo.slug, number, labels, labels.error)
        kind = failure.get("kind", "crash")
        # Provider and crash detail stays in the store; the issue gets the category only.
        public = "" if kind in {"provider", "crash"} else ": " + failure.get("detail", "")[:600]
        issues.comment(repo.slug, number, f"Cardinal stopped at `{failure.get('stage', '?')}` ({kind}){public}")
    return {"run_id": run.run_id, "issue": number, "status": status, "failure": failure or None,
            "branch": run.branch, "pr": values.get("pr"), "merge_sha": values.get("merge_sha")}
