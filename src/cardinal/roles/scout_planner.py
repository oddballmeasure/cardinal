"""Split a repository into survey areas the scout can cover one at a time."""

import json

from langchain_core.tools import tool

from cardinal.agents.backends import under
from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.profile import RepoProfile
from cardinal.contracts.scout import Area, SurveyPlan
from cardinal.graph.context import ScoutRun
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.roles.profiler import relevant_files


def validate(plan: SurveyPlan, tracked: set[str], hidden: list[str]) -> list[Area]:
    for area in plan.areas:
        for path in area.paths:
            clean = path.strip("/")
            if under(clean, hidden) or not any(under(item, [clean]) for item in tracked):
                raise ValueError(f"area {area.name!r} names {path!r}, which holds no tracked file agents may see")
    return plan.areas


def plan(run: ScoutRun, profile: RepoProfile, tracked: set[str]) -> list[Area]:
    @tool("find_repo_context")
    def find_repo_context(query: str) -> str:
        """Return the profiled files most relevant to one behaviour or concern."""
        return json.dumps(relevant_files(profile, query), ensure_ascii=False)

    call = AgentCall(
        stage="scout_planner", model_spec=run.config.models.scout, context_dir=run.context_dir,
        context=run.agent_context(), worktree=run.worktree, repo_writable=False, hidden=run.repo.paths_hidden,
        prompt=("Use the scout-planner skill. The repository is at /repo/ (read-only) and its profile at "
                "/context/repo_profile.json. Return one SurveyPlan."),
        tools=[find_repo_context], schema=SurveyPlan,
    )
    limits = run.config.limits
    problem = ""
    for attempt in (1, 2):
        result = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
        try:
            areas = validate(result, tracked, run.repo.paths_hidden)
        except ValueError as exc:
            problem = str(exc)
            run.recorder.event("scout", "rejected_plan", {"attempt": attempt, "reason": problem})
            call.prompt += f"\n\nYour previous SurveyPlan was rejected: {exc}. Return a corrected SurveyPlan."
            continue
        run.recorder.event("scout", "plan", result.model_dump())
        return areas
    raise StageFailure(FailureKind.INVALID_OUTPUT, f"Scout plan failed validation twice: {problem}")
