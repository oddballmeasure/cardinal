"""Start, resume and settle runs. The CLI and the daemon both come through here."""

import logging
import sqlite3
import uuid
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from cardinal.config.models import Config, Repo
from cardinal.contracts.intake import WORKABLE
from cardinal.github import issues
from cardinal.graph.builder import build
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind
from cardinal.home import Home
from cardinal.logs.context import bind
from cardinal.repo.git import create_worktree, ensure_clone, remove_worktree
from cardinal.roles import orchestrator, profiler
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


def triage(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, number: int) -> dict:
    """Orchestrator intake on a monitor-filed issue: workable goes to ready, anything else, including
    a failed triage, goes to investigate. It never falls through to ready. Tickets are discarded;
    the daemon plans again against the code as it is when it runs."""
    issues.ensure_labels(repo.slug, repo.labels)  # the monitor may file on a repo no daemon has served yet
    issue, _, _ = issues.view(repo.slug, number)
    run_id = uuid.uuid4().hex[:12]
    runs.create(db, run_id, f"{repo.slug}#{number}#{run_id}", repo.slug, number)
    run = Run(home, config, repo, db, Recorder(db, run_id), run_id, issue)
    kind, reason = None, ""
    with bind(run_id=run_id, issue=number, stage="triage"):
        try:
            ensure_clone(run.clone, repo.url, repo.base_branch)
            create_worktree(run.clone, run.worktree, run.branch, repo.base_branch)
            decision, _ = orchestrator.intake(run, profiler.profile(run), None)
            kind, reason = decision.kind, decision.reason
        except Exception as exc:  # noqa: BLE001 - a failed triage parks the issue for investigation
            log.error("triage of #%s failed", number, exc_info=exc, extra={"event": "triage_failed"})
            reason = f"Triage failed ({type(exc).__name__}); investigate before coding."
        finally:
            if run.worktree.exists():
                remove_worktree(run.clone, run.worktree)
        target = repo.labels.ready if kind in WORKABLE else repo.labels.investigate
        runs.update(db, run_id, status="triaged", failure_detail=reason[:2000], ended_at=now())
        issues.set_state(repo.slug, number, repo.labels, target)
        if target != repo.labels.ready:
            issues.comment(repo.slug, number, f"Cardinal triaged this as `{kind or 'failed'}`: {reason}")
        log.info("triaged #%s as %s", number, kind, extra={"event": "issue_triaged", "data": {"label": target, "kind": kind}})
    return {"issue": number, "kind": kind, "label": target}


def drive(run: Run, thread_id: str, payload) -> dict:
    config = {"configurable": {"thread_id": thread_id}}
    with bind(run_id=run.run_id, issue=run.issue.number):
        return _drive(run, config, payload)


def _drive(run: Run, config: dict, payload) -> dict:
    try:
        try:
            with checkpointer(run.home) as saver:
                graph = build(run, saver)
                graph.invoke(payload, config=config)
                snapshot = graph.get_state(config)
            values, paused = snapshot.values, bool(snapshot.next)
        except Exception as exc:  # noqa: BLE001 - the graph itself failed; the run row and labels still settle
            log.exception("run %s crashed outside a node", run.run_id, extra={"event": "run_crashed",
                                                                               "failure_kind": FailureKind.CRASH.value})
            values = {"status": "failed", "failure": {"kind": FailureKind.CRASH.value, "detail": repr(exc)[:2000],
                                                      "stage": "graph"}}
            paused = False
        except (KeyboardInterrupt, SystemExit):
            log.warning("run %s interrupted", run.run_id, extra={"event": "run_interrupted"})
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
    log.info("run %s settled %s", run.run_id, status, extra={"event": "run_settled", "failure_kind": failure.get("kind"),
                                                             "data": {"status": status, "stage": failure.get("stage")}})
    if status == "done":
        issues.set_state(repo.slug, number, labels, labels.done)
        pr = values.get("pr") or {}
        issues.comment(repo.slug, number, f"Cardinal resolved this in {pr.get('url', 'its pull request')}"
                       f" (merge `{(values.get('merge_sha') or '')[:12]}`).")
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
