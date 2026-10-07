"""Graph nodes: thin wrappers that call a role and turn its outcome into state.

The live dependencies arrive as `run`, bound with functools.partial. Never name that parameter
`runtime`: LangGraph injects its own object into a parameter with that name.
"""

import functools
import logging
import subprocess
import traceback

from langgraph.types import interrupt

from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision, Ticket
from cardinal.contracts.verdict import Verdict
from cardinal.github.gh import GhError
from cardinal.github.prs import failure_log
from cardinal.graph import runtime
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.graph.state import RunState
from cardinal.logs.context import bind
from cardinal.repo.git import GitError, create_worktree, ensure_clone, publish
from cardinal.roles import deployer, orchestrator, pr_manager, profiler, verifier
from cardinal.store import inputs, runs

log = logging.getLogger(__name__)


def guarded(stage: str):
    """Every failure leaves the node as a typed failure in state, never as an escaped exception."""
    def decorate(node):
        @functools.wraps(node)
        def wrapper(state: RunState, run: Run) -> dict:
            with bind(stage=stage):
                try:
                    return node(state, run)
                except StageFailure as exc:
                    kind, detail, error = exc.kind, exc.detail, exc
                except (GitError, subprocess.CalledProcessError) as exc:
                    kind, detail, error = FailureKind.GIT, str(exc), exc
                except GhError as exc:
                    kind, detail, error = FailureKind.GITHUB, str(exc), exc
                except Exception as exc:  # noqa: BLE001 - a Cardinal defect; recorded with its traceback
                    kind, error = FailureKind.CRASH, exc
                    detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-3000:]}"
                # A StageFailure raised from a lower error carries that error's stack, where the defect is.
                cause = error.__cause__ if isinstance(error, StageFailure) and error.__cause__ else error
                log.log(kind.level, "stage %s failed (%s): %s", stage, kind.value, detail[:500], exc_info=cause,
                        extra={"event": "stage_failed", "failure_kind": kind.value})
            run.recorder.event(stage, "failure", {"kind": kind.value, "detail": detail})
            return {"status": "failed", "failure": {"kind": kind.value, "detail": detail, "stage": stage}}
        return wrapper
    return decorate


@guarded("profile")
def profile_node(state: RunState, run: Run) -> dict:
    ensure_clone(run.clone, run.repo.url, run.repo.base_branch)
    base_sha = state.get("base_sha")
    if not run.worktree.exists():
        base_sha = create_worktree(run.clone, run.worktree, run.branch, run.repo.base_branch)
        runs.update(run.db, run.run_id, branch=run.branch)
    result = profiler.profile(run)
    return {"status": "running", "base_sha": base_sha, "profile_revision": result.revision}


@guarded("intake")
def intake_node(state: RunState, run: Run) -> dict:
    profile = inputs.load_profile(run.db, run.repo.slug)
    if profile is None:
        raise StageFailure(FailureKind.CRASH, "The profile vanished between profiling and intake")
    decision, tickets = orchestrator.intake(run, profile, state.get("human_answer"))
    update = {"decision": decision.model_dump(), "tickets": [ticket.model_dump() for ticket in tickets]}
    if decision.kind == "needs_human":
        return {**update, "status": "awaiting_human", "question": decision.reason}
    if not decision.workable:
        return {**update, "status": "rejected"}
    return {**update, "status": "running", "verify_round": 1, "repair": None}


def human_node(state: RunState, run: Run) -> dict:
    """Pause until `cardinal resume`. Its own node, so resuming never repeats the orchestrator's call."""
    answer = interrupt({"question": state.get("question", "")})
    run.recorder.event("human", "answer", answer)
    if answer.get("action") == "reject":
        return {"status": "rejected", "human_answer": answer.get("note")}
    return {"status": "running", "human_answer": answer.get("note") or "Approved; proceed as you judge best."}


@guarded("implement")
def implement_node(state: RunState, run: Run) -> dict:
    decision = IntakeDecision.model_validate(state["decision"])
    tickets = [Ticket.model_validate(item) for item in state["tickets"]]
    base_sha = runtime.fresh_base(run, state["base_sha"])
    repair = None
    if state.get("sync"):
        repair = runtime.sync_ticket(decision, tickets, run.repo.base_branch, state["sync"], state["sync_round"])
    elif state.get("ci_repair"):
        repair = runtime.ci_repair_ticket(decision, tickets, state["ci_repair"], state["ci_round"])
    elif state.get("repair"):
        repair = runtime.repair_ticket(decision, tickets, state["repair"], state.get("verify_round", 1))
    head_sha, tests = runtime.implement(run, decision, tickets, base_sha, repair)
    runs.update(run.db, run.run_id, head_sha=head_sha)
    return {"base_sha": base_sha, "head_sha": head_sha, "head_tests": tests, "repair": None, "ci_repair": None,
            "sync": None}


@guarded("verify")
def verify_node(state: RunState, run: Run) -> dict:
    decision = IntakeDecision.model_validate(state["decision"])
    tests = state.get("head_tests") or {}
    evidence = (CommandEvidence.model_validate({k: v for k, v in tests.items() if k != "sha"})
                if tests.get("sha") == state["head_sha"] else None)
    verdict = verifier.verify(run, decision, state["base_sha"], evidence)
    if verdict.approved:
        return {"verdict": verdict.model_dump()}
    round_number = state.get("verify_round", 1)
    if round_number >= run.config.limits.verify_rounds:
        raise StageFailure(FailureKind.VERIFICATION, "Verifier findings remain after "
                           f"{round_number} rounds: {verdict.findings}")
    return {"verdict": verdict.model_dump(), "repair": verdict.findings, "verify_round": round_number + 1}


@guarded("sync")
def sync_node(state: RunState, run: Run) -> dict:
    update = runtime.sync_base(run, state)
    if update.get("head_sha"):
        runs.update(run.db, run.run_id, head_sha=update["head_sha"])
    return update


@guarded("publish")
def publish_node(state: RunState, run: Run) -> dict:
    pushed = publish(run.worktree, run.branch)
    if pushed != state["verdict"]["head_sha"]:
        raise StageFailure(FailureKind.GIT, f"Pushed {pushed}, but the verdict covers {state['verdict']['head_sha']}")
    return {}


@guarded("pr")
def pr_node(state: RunState, run: Run) -> dict:
    try:
        result = pr_manager.land(run, Verdict.model_validate(state["verdict"]))
    except pr_manager.BaseConflict as exc:
        if state.get("sync_round", 0) >= run.config.limits.base_sync_rounds:
            raise
        run.recorder.event("pr", "conflict", {"detail": exc.detail})
        return {"resync": True}  # back to sync: merge the base in, then implement or verify again
    except pr_manager.CIFailed as exc:
        used = state.get("ci_round", 0)
        if used >= run.config.limits.ci_repair_rounds:
            raise
        log = failure_log(run.repo.slug, exc.head_sha, exc.failed)
        run.recorder.event("pr", "ci_repair", {"round": used + 1, "head_sha": exc.head_sha, "log": log[-2000:]})
        # Back to the coder with the log; the verifier then gets its full rounds on the repaired head.
        return {"ci_repair": [log], "ci_round": used + 1, "verify_round": 1}
    runs.update(run.db, run.run_id, pr_number=result.number, pr_url=result.url, merge_sha=result.merge_sha)
    return {"pr": result.model_dump(), "merge_sha": result.merge_sha}


@guarded("deploy")
def deploy_node(state: RunState, run: Run) -> dict:
    result = deployer.deploy(run, state["merge_sha"])
    runs.update(run.db, run.run_id, deployed_sha=result.deployed_revision)
    return {"deployment": result.model_dump()}


def done_node(state: RunState, run: Run) -> dict:
    return {"status": "done"}
