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
from cardinal.locks import lock
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


def start(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, number: int, *, daemon: bool = False) -> dict:
    """Run one issue from the top. Refuses an issue another run has claimed."""
    issue, _, state = issues.view(repo.slug, number)
    if state != "OPEN":
        raise ValueError(f"Issue #{number} is {state.lower()}")
    run_id = uuid.uuid4().hex[:12]
    if not inputs.claim(db, repo.slug, number, run_id):
        raise ValueError(f"Issue #{number} is already claimed by another run")
    thread_id = f"{repo.slug}#{number}#{run_id}"
    changing_label = False
    try:
        runs.create(db, run_id, thread_id, repo.slug, number)
        changing_label = True
        issues.set_state(repo.slug, number, repo.labels, repo.labels.working)
    except BaseException as exc:
        # A failed edit may have applied only its add-label half. Keep the claim until the
        # state axis is reconciled, so another poll cannot start a second run mid-repair.
        try:
            try:
                if runs.get(db, run_id)["status"] == "running":
                    interrupted = daemon and isinstance(exc, (KeyboardInterrupt, SystemExit))
                    runs.update(db, run_id, status="interrupted" if interrupted else "failed",
                                failure_kind=None if interrupted else FailureKind.SETUP.value,
                                failure_detail=None if interrupted else repr(exc)[:2000], ended_at=now())
            except KeyError:  # insertion failed before there was a row to settle
                pass
            if changing_label:
                try:
                    issues.set_state(repo.slug, number, repo.labels, repo.labels.ready)
                except Exception:
                    log.exception("could not restore ready label after setup failed for #%s", number,
                                  extra={"event": "setup_label_recovery_failed"})
        finally:
            inputs.release(db, repo.slug, number)
        raise
    run = Run(home, config, repo, db, Recorder(db, run_id), run_id, issue)
    keep_claim = False
    try:
        run.recorder.event("run", "started", {"issue": issue.model_dump(), "thread_id": thread_id})
        return drive(run, thread_id, {"status": "running"}, daemon=daemon)
    except (KeyboardInterrupt, SystemExit):
        if daemon:
            try:
                interrupt(repo, db, run_id, number)
            except Exception:
                # A daemon stop always wins. Keep the claim, as interrupt() promises, so the next
                # daemon's orphaned-claim recovery settles the issue the label work could not.
                keep_claim = True
                log.exception("could not restore labels after daemon stop for #%s", number,
                              extra={"event": "interrupt_label_recovery_failed"})
        raise
    finally:
        if not keep_claim:
            inputs.release(db, repo.slug, number)


def resume(home: Home, config: Config, repo: Repo, db: sqlite3.Connection, number: int, action: str, note: str) -> dict:
    row = runs.latest(db, repo.slug, number)
    if row is None or row["status"] != "awaiting_human":
        raise ValueError(f"Issue #{number} has no run waiting for a person")
    if not inputs.claim(db, repo.slug, number, row["run_id"]):
        raise ValueError(f"Issue #{number} is already claimed by another run")
    marked_running = changing_label = False
    try:
        issue, _, _ = issues.view(repo.slug, number)
        runs.update(db, row["run_id"], status="running")
        marked_running = True
        changing_label = True
        issues.set_state(repo.slug, number, repo.labels, repo.labels.working)
    except BaseException:
        # Put the run back where settle() left it, so the person can resume it again. A failed edit
        # may have applied, so restore the needs-human label (best effort) before releasing the claim.
        try:
            if marked_running:
                runs.update(db, row["run_id"], status="awaiting_human")
            if changing_label:
                try:
                    issues.set_state(repo.slug, number, repo.labels, repo.labels.needs_human)
                except Exception:
                    log.exception("could not restore needs-human label after resume setup failed for #%s", number,
                                  extra={"event": "resume_label_recovery_failed"})
        finally:
            inputs.release(db, repo.slug, number)
        raise
    run = Run(home, config, repo, db, Recorder(db, row["run_id"]), row["run_id"], issue)
    try:
        return drive(run, row["thread_id"], Command(resume={"action": action, "note": note}))
    finally:
        inputs.release(db, repo.slug, number)


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
            with lock(run.clone, "intake"):
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


def abandon(repo: Repo, db: sqlite3.Connection, claim: sqlite3.Row) -> dict:
    """Settle a run whose process died without settling (SIGKILL, out of memory): its claim would
    otherwise hold the issue forever. Its worktree stays; a later run of the issue replaces it."""
    number, run_id = claim["issue"], claim["run_id"]
    if runs.is_settled(db, run_id):
        # The process died after settle() recorded the outcome: keep it, finish only what is missing.
        with bind(run_id=run_id, issue=number):
            try:
                finish_settled(repo, db, run_id, number)
            finally:
                inputs.release(db, repo.slug, number)
        settled = runs.settled_event(db, run_id) or {}
        return {"run_id": run_id, "issue": number, "status": runs.get(db, run_id)["status"],
                "failure": settled.get("failure") or None}
    detail = f"The process running this issue (pid {claim['pid']}) died without settling"
    failure = {"kind": FailureKind.CRASH.value, "stage": "abandoned", "detail": detail}
    with bind(run_id=run_id, issue=number):
        log.error("run %s abandoned: %s", run_id, detail, extra={"event": "run_abandoned",
                                                                 "failure_kind": FailureKind.CRASH.value})
        try:
            runs.update(db, run_id, status="failed", failure_kind=failure["kind"], failure_detail=detail, ended_at=now())
            issues.set_state(repo.slug, number, repo.labels, repo.labels.error)
            issues.comment(repo.slug, number, f"Cardinal stopped at `abandoned` (crash): {detail}")
        finally:
            inputs.release(db, repo.slug, number)
    return {"run_id": run_id, "issue": number, "status": "failed", "failure": failure}


def interrupt(repo: Repo, db: sqlite3.Connection, run_id: str, number: int) -> None:
    """A daemon stop abandons this attempt, not the issue. Keep its worktree for the next
    fresh run to replace, and keep the claim until the ready label has been restored."""
    if runs.is_settled(db, run_id):
        finish_settled(repo, db, run_id, number)
        return
    runs.update(db, run_id, status="interrupted", ended_at=now())
    Recorder(db, run_id).event("run", "interrupted", {})
    issues.set_state(repo.slug, number, repo.labels, repo.labels.ready)
    log.warning("run %s interrupted by daemon shutdown", run_id, extra={"event": "run_interrupted"})


def finish_settled(repo: Repo, db: sqlite3.Connection, run_id: str, number: int) -> None:
    """A stop landed after settle() recorded the outcome. The run row stays as settled; only the
    label and comment settle() had not yet written are completed, never duplicated."""
    row = runs.get(db, run_id)
    labels = repo.labels
    log.warning("run %s was stopped after it settled %s; finishing its label", run_id, row["status"],
                extra={"event": "run_settled_then_stopped"})
    if row["status"] == "done":
        issues.set_state(repo.slug, number, labels, labels.done)
        if not any(body.startswith("Cardinal resolved this") for body in issues.comment_bodies(repo.slug, number)):
            issues.comment(repo.slug, number, f"Cardinal resolved this in {row['pr_url'] or 'its pull request'}"
                           f" (merge `{(row['merge_sha'] or '')[:12]}`).")
    elif row["status"] in ("awaiting_human", "rejected"):
        issues.set_state(repo.slug, number, labels, labels.needs_human)
        if not any(body.startswith((NEEDS_DECISION, NOT_TAKING)) for body in issues.comment_bodies(repo.slug, number)):
            settled = runs.settled_event(db, run_id) or {}
            if row["status"] == "awaiting_human":
                issues.comment(repo.slug, number, question_comment(number, settled.get("question") or ""))
            else:
                issues.comment(repo.slug, number, rejected_comment(settled.get("reason", "No reason recorded.")))
    elif row["status"] == "failed":
        issues.set_state(repo.slug, number, labels, labels.error)
        if not any(body.startswith(STOPPED_AT) for body in issues.comment_bodies(repo.slug, number)):
            settled = runs.settled_event(db, run_id) or {}
            failure = settled.get("failure")
            if not isinstance(failure, dict) or not failure:
                failure = {key: value for key, value in (("kind", row["failure_kind"]),
                                                         ("detail", row["failure_detail"])) if value is not None}
            issues.comment(repo.slug, number, stopped_comment(failure))


STOPPED_AT = "Cardinal stopped at"


def stopped_comment(failure: dict) -> str:
    """The issue comment for a run that settled `failed`; settle() and finish_settled() share it."""
    kind = failure.get("kind", "crash")
    # Provider and crash detail stays in the store; the issue gets the category only.
    public = "" if kind in {"provider", "crash"} else ": " + (failure.get("detail") or "")[:600]
    return f"{STOPPED_AT} `{failure.get('stage', '?')}` ({kind}){public}"


NEEDS_DECISION = "Cardinal needs a decision before continuing"
NOT_TAKING = "Cardinal is not taking this issue:"


def question_comment(number: int, question: str) -> str:
    """The issue comment for a run that settled `awaiting_human`; settle() and finish_settled() share it."""
    return (f"{NEEDS_DECISION}:\n\n> " + question.replace("\n", "\n> ")
            + "\n\nAnswer with `cardinal resume " + str(number) + " --approve --note \"...\"` or `--reject`.")


def rejected_comment(reason: str) -> str:
    """The issue comment for a run that settled `rejected`; settle() and finish_settled() share it."""
    return f"{NOT_TAKING} {reason}"


def drive(run: Run, thread_id: str, payload, *, daemon: bool = False) -> dict:
    # Each verify and CI repair round revisits nodes; LangGraph's default of 25 steps is too tight.
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    with bind(run_id=run.run_id, issue=run.issue.number):
        return _drive(run, config, payload, daemon=daemon)


def _drive(run: Run, config: dict, payload, *, daemon: bool = False) -> dict:
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
        if not daemon:
            log.warning("run %s interrupted", run.run_id, extra={"event": "run_interrupted"})
            # Foreground runs keep the original interrupted-crash behavior.
            settle(run, {"status": "failed", "failure": {"kind": FailureKind.CRASH.value, "stage": "interrupted",
                                                     "detail": "Cardinal was stopped during this run"}}, False)
        raise
    return settle(run, values, paused)


def settle(run: Run, values: dict, paused: bool) -> dict:
    repo, number, labels = run.repo, run.issue.number, run.repo.labels
    status = "awaiting_human" if paused else values.get("status", "failed")
    failure = values.get("failure") or {}
    runs.update(run.db, run.run_id, status=status, failure_kind=failure.get("kind"),
                failure_detail=failure.get("detail"), ended_at=None if paused else now())
    settled = {"status": status, "failure": failure}
    question = values.get("question", "")
    reason = (values.get("decision") or {}).get("reason", "No reason recorded.")
    if status == "awaiting_human":
        settled["question"] = question
    elif status == "rejected":
        settled["reason"] = reason
    run.recorder.event("run", "settled", settled)
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
        issues.comment(repo.slug, number, question_comment(number, question))
    elif status == "rejected":
        issues.set_state(repo.slug, number, labels, labels.needs_human)
        issues.comment(repo.slug, number, rejected_comment(reason))
    else:
        issues.set_state(repo.slug, number, labels, labels.error)
        issues.comment(repo.slug, number, stopped_comment(failure))
    return {"run_id": run.run_id, "issue": number, "status": status, "failure": failure or None,
            "branch": run.branch, "pr": values.get("pr"), "merge_sha": values.get("merge_sha")}
