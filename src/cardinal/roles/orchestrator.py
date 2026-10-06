"""Intake and planning in one call: decide whether Cardinal should do this issue, and how."""

import json

from langchain_core.tools import tool

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.intake import IntakeDecision, Ticket
from cardinal.contracts.profile import RepoProfile
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.roles.profiler import dump, relevant_files


def ordered(decision: IntakeDecision) -> list[Ticket]:
    """Tickets in dependency order. Raises ValueError with a reason the orchestrator can act on."""
    ids = [item.id for item in decision.requirements]
    if len(ids) != len(set(ids)):
        raise ValueError("requirement IDs must be unique")
    by_id = {ticket.id: ticket for ticket in decision.tickets}
    if len(by_id) != len(decision.tickets):
        raise ValueError("ticket IDs must be unique")
    covered = [item for ticket in decision.tickets for item in ticket.covers]
    if sorted(covered) != sorted(ids):
        raise ValueError(f"tickets must cover every requirement exactly once; requirements {ids}, covered {covered}")
    for ticket in decision.tickets:
        if ticket.source_issue != decision.issue_number:
            raise ValueError(f"ticket {ticket.id} must name source_issue {decision.issue_number}")
        if not set(ticket.depends_on) <= by_id.keys() - {ticket.id}:
            raise ValueError(f"ticket {ticket.id} depends on an unknown ticket")
    result: list[Ticket] = []
    remaining = dict(by_id)
    while remaining:
        ready = [ticket for ticket in remaining.values() if set(ticket.depends_on) <= {item.id for item in result}]
        if not ready:
            raise ValueError("ticket dependencies form a cycle")
        for ticket in ready:
            result.append(ticket)
            del remaining[ticket.id]
    return result


def validate(decision: IntakeDecision, run: Run, profile: RepoProfile, queried: list) -> list[Ticket]:
    if decision.issue_number != run.issue.number:
        raise ValueError(f"issue_number must be {run.issue.number}")
    if not decision.workable:
        return []
    tickets = ordered(decision)
    known = {entry.path for entry in profile.files}
    for ticket in tickets:
        unknown = sorted(set(ticket.target_files) - known)
        if unknown:
            raise ValueError(f"ticket {ticket.id} targets paths absent from the profile: {unknown}")
    if not queried:
        raise ValueError("query find_repo_context for each requested behavior before planning")
    return tickets


def intake(run: Run, profile: RepoProfile, human_answer: str | None) -> tuple[IntakeDecision, list[Ticket]]:
    run.write_context("issue.json", run.issue.model_dump())
    run.write_context("repo_profile.json", dump(profile))
    if human_answer:
        run.write_context("human_answer.json", {"answer": human_answer})
    queried: list[dict] = []

    @tool("find_repo_context")
    def find_repo_context(query: str) -> str:
        """Return the profiled files most relevant to one requested behavior."""
        matches = relevant_files(profile, query)
        queried.append({"query": query, "paths": [item["path"] for item in matches]})
        return json.dumps(matches, ensure_ascii=False)

    answer_note = (" A person has answered your earlier question; read /context/human_answer.json and do not ask "
                   "the same question again.") if human_answer else ""
    call = AgentCall(
        stage="orchestrator", model_spec=run.config.models.orchestrator, context_dir=run.context_dir,
        context=run.agent_context(human_answer=human_answer), worktree=run.worktree, repo_writable=False,
        hidden=run.repo.paths_hidden,
        prompt=("Use the orchestrator skill. Read /context/issue.json and decide how Cardinal should handle it. "
                "The repository is at /repo/ (read-only) and its profile at /context/repo_profile.json. "
                "Return one IntakeDecision." + answer_note),
        tools=[find_repo_context], schema=IntakeDecision,
    )
    limits = run.config.limits
    problem = ""
    for attempt in (1, 2):
        decision = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
        try:
            tickets = validate(decision, run, profile, queried)
        except ValueError as exc:
            problem = str(exc)
            run.recorder.event("intake", "rejected_decision", {"attempt": attempt, "reason": problem})
            call.prompt += f"\n\nYour previous IntakeDecision was rejected: {exc}. Return a corrected IntakeDecision."
            continue
        run.recorder.event("intake", "decision", {**decision.model_dump(), "queries": queried})
        return decision, tickets
    raise StageFailure(FailureKind.INVALID_OUTPUT, f"Orchestrator decision failed validation twice: {problem}")
