"""One coding attempt on one ticket. The runtime decides afterwards whether it worked."""

from langchain_core.tools import tool

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision, Ticket
from cardinal.graph.context import Run
from cardinal.repo.git import fingerprint
from cardinal.roles.tests import run_repo_tests, summary


def attempt(run: Run, decision: IntakeDecision, ticket: Ticket, number: int, feedback: str | None) -> list[tuple[str, CommandEvidence]]:
    run.write_context("issue.json", run.issue.model_dump())
    run.write_context("ticket.json", ticket.model_dump())
    run.write_context("requirements.json", [item.model_dump() for item in decision.requirements])
    run.write_context("feedback.md", feedback or "No earlier attempt.\n")
    runs: list[tuple[str, CommandEvidence]] = []

    @tool("run_repo_tests")
    def run_repo_tests_tool() -> str:
        """Run the repository's own test command in /repo/ and return its result."""
        tree = fingerprint(run.worktree)
        evidence = run_repo_tests(run.worktree, run.repo.test_command, run.config.limits.test_timeout_seconds)
        runs.append((tree, evidence))
        return summary(evidence)

    retry_note = (f" This is attempt {number}; /context/feedback.md explains why the previous attempt was not accepted."
                  if feedback else "")
    call = AgentCall(
        stage="coder", model_spec=run.config.models.coder, context_dir=run.context_dir, ticket_id=ticket.id,
        context=run.agent_context(ticket=ticket.model_dump(), attempt=number, feedback=feedback),
        worktree=run.worktree, repo_writable=True, off_limits=run.repo.paths_off_limits,
        prompt=(f"Use the coder skill to implement ticket {ticket.id} in /repo/. Read /context/ticket.json, "
                "/context/issue.json and /context/requirements.json. Add tests to the repository that exercise "
                "the new behavior through the application's boundary, then call run_repo_tests and fix failures "
                "before you finish." + retry_note),
        tools=[run_repo_tests_tool],
    )
    limits = run.config.limits
    run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
    return runs
