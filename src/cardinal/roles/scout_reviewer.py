"""An independent reviewer re-derives one proposal from the code before it can be filed."""

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.scout import Proposal, ProposalReview
from cardinal.graph.context import ScoutRun


def review(run: ScoutRun, proposal: Proposal) -> ProposalReview:
    run.write_context("proposal.json", proposal.model_dump())
    call = AgentCall(
        stage="scout_reviewer", model_spec=run.config.models.scout_reviewer, context_dir=run.context_dir,
        context=run.agent_context(proposal=proposal.model_dump()), worktree=run.worktree, repo_writable=False,
        hidden=run.repo.paths_hidden,
        prompt=("Use the scout-reviewer skill. Judge the proposal in /context/proposal.json against the code at "
                "/repo/ (read-only) and the issues in /context/issues.json. Return one ProposalReview."),
        schema=ProposalReview,
    )
    limits = run.config.limits
    result = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
    run.recorder.event("scout", "review", {"title": proposal.title, "verdict": result.verdict, "reason": result.reason})
    return result
