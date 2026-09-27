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
from cardinal.repo.git import commit_all, dirty, fingerprint, git, head, reset_to, ticket_commits
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


def fresh_base(run: Run, base_sha: str) -> str:
    """With nothing accepted yet, restart from the newest base so merges since profiling are included."""
    if ticket_commits(run.worktree, base_sha):
        return base_sha
    git(run.worktree, "fetch", "-q", "origin", run.repo.base_branch, timeout=600)
    newest = git(run.worktree, "rev-parse", f"refs/remotes/origin/{run.repo.base_branch}").strip()
    reset_to(run.worktree, newest)
    return newest


def implement(run: Run, decision: IntakeDecision, tickets: list[Ticket], base_sha: str,
              repair: list[str] | None, round_number: int) -> tuple[str, dict]:
    """Returns the branch head and the test evidence observed at exactly that head."""
    done = ticket_commits(run.worktree, base_sha)
    work = [ticket for ticket in tickets if ticket.id not in done]
    if repair:
        work.append(repair_ticket(decision, tickets, repair, round_number))
    evidence: CommandEvidence | None = None
    for ticket in work:
        evidence = implement_ticket(run, decision, ticket)
    return head(run.worktree), {"sha": head(run.worktree), **evidence.model_dump()} if evidence else {}


def implement_ticket(run: Run, decision: IntakeDecision, ticket: Ticket) -> CommandEvidence:
    feedback: str | None = None
    attempts = run.config.limits.coder_attempts
    for number in range(1, attempts + 1):
        agent_runs = coder.attempt(run, decision, ticket, number, feedback)
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
