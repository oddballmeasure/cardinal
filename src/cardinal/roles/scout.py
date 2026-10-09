"""Survey one area of a repository for small, verifiable work worth filing."""

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.scout import Area, ScoutFindings
from cardinal.graph.context import ScoutRun


def survey(run: ScoutRun, area: Area) -> ScoutFindings:
    run.write_context("area.json", area.model_dump())
    call = AgentCall(
        stage="scout", model_spec=run.config.models.scout, context_dir=run.context_dir,
        context=run.agent_context(area=area.model_dump()), worktree=run.worktree, repo_writable=False,
        hidden=run.repo.paths_hidden,
        prompt=("Use the scout skill. Survey the area in /context/area.json. The repository is at /repo/ "
                "(read-only); /context/ holds its profile, the scout's rules, existing issues and rejected "
                "proposals. Return one ScoutFindings."),
        schema=ScoutFindings,
    )
    limits = run.config.limits
    findings = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
    run.recorder.event("scout", "findings", {"area": area.name, "proposals": [item.title for item in findings.proposals]})
    return findings
