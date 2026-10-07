"""The ticket and attempt loop.

It lives in one function rather than across graph nodes because it is one unit of work with one
owner: a checkpoint taken between attempts could resume against a worktree that no longer matches
it. Progress that must survive a crash is kept in git itself: each accepted ticket is a commit with
a `Cardinal-Ticket:` trailer, and re-entry skips tickets already committed.
"""

from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision, Ticket
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.repo.git import (commit_all, dirty, fetch_base, fingerprint, head, is_ancestor, merge, reset_to,
                               ticket_commits, unresolved, would_conflict)
from cardinal.roles import coder
from cardinal.roles.tests import run_repo_tests, summary


def repair_ticket(decision: IntakeDecision, tickets: list[Ticket], findings: list[str], round_number: int) -> Ticket:
    targets = sorted({path for ticket in tickets for path in ticket.target_files})
    return Ticket(
        id=f"REPAIR-{round_number}", source_issue=decision.issue_number,
        title="Resolve the verifier's findings",
        task="The verifier rejected the branch. Resolve every finding below without regressing earlier work:\n- "
             + "\n- ".join(findings),
        acceptance_criteria=["Every finding is resolved", "The repository's test command passes"],
        covers=[item.id for item in decision.requirements], depends_on=[], target_files=targets,
    )


def ci_repair_ticket(decision: IntakeDecision, tickets: list[Ticket], logs: list[str], round_number: int) -> Ticket:
    targets = sorted({path for ticket in tickets for path in ticket.target_files})
    return Ticket(
        id=f"CI-REPAIR-{round_number}", source_issue=decision.issue_number,
        title="Make the required CI checks pass",
        task=("The branch passed the repository's test command here, but a required CI check failed on the "
              "pushed commit. CI runs in a different environment (often Linux, a different Python build, "
              "a different clock or timezone), so look for code or tests that depend on the machine. "
              "Fix the cause shown in the log; do not weaken or skip the failing test.\n\n" + "\n\n".join(logs)),
        acceptance_criteria=["The failure shown in the CI log is fixed at its cause",
                             "The repository's test command passes"],
        covers=[item.id for item in decision.requirements], depends_on=[], target_files=targets,
    )


def sync_ticket(decision: IntakeDecision, tickets: list[Ticket], base: str, sync: dict, round_number: int) -> Ticket:
    if sync.get("conflicts"):
        task = (f"Cardinal merged the latest `{base}` into this branch and these files conflict: {sync['conflicts']}. "
                "Each conflict is marked with <<<<<<<, ======= and >>>>>>> lines. Resolve every one so the file keeps "
                f"the intent of both sides, this branch's change and the change that landed on `{base}`, and remove "
                "every marker. Change nothing else.")
        targets = sync["conflicts"]
    else:
        task = (f"Cardinal merged the latest `{base}` into this branch without conflicts, but the repository's test "
                f"command now fails. Make it pass, keeping both this branch's change and what landed on `{base}`.\n\n"
                + sync["tests"])
        targets = sorted({path for ticket in tickets for path in ticket.target_files})
    return Ticket(
        id=f"SYNC-{round_number}", source_issue=decision.issue_number, title=f"Bring the branch up to date with {base}",
        task=task, acceptance_criteria=["Both sides' changes are kept", "The repository's test command passes"],
        covers=[item.id for item in decision.requirements], depends_on=[], target_files=targets,
    )


def sync_base(run: Run, state: dict) -> dict:
    """Merge a moved base into the verified branch before it is published. A clean merge is tested here
    and verified again; a conflict or a failing test goes back to implement as a SYNC ticket. Only a
    conflict left after base_sync_rounds merges fails the run."""
    base, rounds = run.repo.base_branch, state.get("sync_round", 0)
    newest = fetch_base(run.clone, run.worktree, base)
    if is_ancestor(run.worktree, newest, "HEAD"):
        if state.get("resync"):
            raise StageFailure(FailureKind.BASE_CONFLICT, f"GitHub reports a conflict, but the branch already contains {base}")
        return {}
    if rounds >= run.config.limits.base_sync_rounds:
        if state.get("resync") or would_conflict(run.worktree, "HEAD", newest):
            raise StageFailure(FailureKind.BASE_CONFLICT, f"The branch still conflicts with {base} after {rounds} sync rounds")
        run.recorder.event("sync", "skipped", {"base_sha": newest, "rounds": rounds})
        return {}  # out of rounds but mergeable: GitHub can merge it as it is
    conflicts = merge(run.worktree, newest, f"Merge {base} into {run.branch}")
    run.recorder.event("sync", "merged", {"round": rounds + 1, "base_sha": newest, "conflicts": conflicts})
    update = {"base_sha": newest, "sync_round": rounds + 1, "verify_round": 1, "resync": False}
    if conflicts:
        return {**update, "sync": {"conflicts": conflicts}}
    evidence = run_repo_tests(run.worktree, run.repo.test_command, run.config.limits.test_timeout_seconds)
    if not evidence.passed:
        return {**update, "sync": {"tests": summary(evidence)}}
    sha = head(run.worktree)
    return {**update, "head_sha": sha, "head_tests": {"sha": sha, **evidence.model_dump()}}


def fresh_base(run: Run, base_sha: str) -> str:
    """With nothing accepted yet, restart from the newest base so merges since profiling are included."""
    if ticket_commits(run.worktree, base_sha):
        return base_sha
    newest = fetch_base(run.clone, run.worktree, run.repo.base_branch)
    reset_to(run.worktree, newest)
    return newest


def implement(run: Run, decision: IntakeDecision, tickets: list[Ticket], base_sha: str,
              repair: Ticket | None) -> tuple[str, dict]:
    """Returns the branch head and the test evidence observed at exactly that head."""
    done = ticket_commits(run.worktree, base_sha)
    work = [ticket for ticket in tickets if ticket.id not in done]
    if repair:
        work.append(repair)
    evidence: CommandEvidence | None = None
    for ticket in work:
        evidence = implement_ticket(run, decision, ticket)
    return head(run.worktree), {"sha": head(run.worktree), **evidence.model_dump()} if evidence else {}


def implement_ticket(run: Run, decision: IntakeDecision, ticket: Ticket) -> CommandEvidence:
    feedback: str | None = None
    attempts = run.config.limits.coder_attempts
    for number in range(1, attempts + 1):
        agent_runs = coder.attempt(run, decision, ticket, number, feedback)
        left = unresolved(run.worktree)
        if left:
            feedback = f"Conflict markers remain in {left}. Resolve every conflict in /repo/ and remove the markers."
            run.recorder.event("implement", "attempt", {"ticket": ticket.id, "attempt": number, "outcome": "conflicted"})
            if number == attempts:
                raise StageFailure(FailureKind.BASE_CONFLICT, f"Ticket {ticket.id} left conflict markers in {left}")
            continue
        if not dirty(run.worktree):
            feedback = "The previous attempt changed no files. Implement the ticket in /repo/."
            run.recorder.event("implement", "attempt", {"ticket": ticket.id, "attempt": number, "outcome": "no_change"})
            if number == attempts:
                raise StageFailure(FailureKind.NO_CHANGE, f"Ticket {ticket.id} produced no change in {attempts} attempts")
            continue
        tree = fingerprint(run.worktree)
        reused = next((result for seen, result in reversed(agent_runs) if seen == tree), None)
        evidence = reused or run_repo_tests(run.worktree, run.repo.test_command, run.config.limits.test_timeout_seconds)
        run.recorder.event("implement", "attempt", {"ticket": ticket.id, "attempt": number, "passed": evidence.passed,
                                                    "reused_agent_test_run": reused is not None,
                                                    "exit_code": evidence.exit_code})
        if evidence.passed:
            sha = commit_all(run.worktree, f"{ticket.title}\n\nCardinal-Ticket: {ticket.id}\nRefs: #{run.issue.number}")
            run.recorder.event("implement", "committed", {"ticket": ticket.id, "sha": sha})
            return evidence
        feedback = ("The repository's test command failed after the previous attempt. The changes are still in "
                    "/repo/; fix them.\n\n" + summary(evidence))
    raise StageFailure(FailureKind.TESTS_FAILED, f"Ticket {ticket.id}: repository tests still fail after {attempts} attempts")
