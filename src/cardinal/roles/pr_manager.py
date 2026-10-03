"""Open or reuse the PR, wait for required checks on the verified head, merge."""

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.pr import PRResult
from cardinal.contracts.verdict import Verdict
from cardinal.github.prs import PRService
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.repo.git import remote_sha


class CIFailed(StageFailure):
    """Required checks failed on the verified head. The runtime may send their log back to the coder."""

    def __init__(self, detail: str, head_sha: str) -> None:
        super().__init__(FailureKind.CI_FAILED, detail)
        self.head_sha = head_sha


def land(run: Run, verdict: Verdict) -> PRResult:
    limits = run.config.limits
    service = PRService(run.clone, run.repo.slug, run.repo.base_branch, run.branch, verdict,
                        run.repo.required_checks, limits.ci_timeout_seconds, limits.ci_poll_seconds,
                        run.issue.number)
    run.write_context("verdict.json", verdict.model_dump())
    call = AgentCall(
        stage="pr_manager", model_spec=run.config.models.pr_manager, context_dir=run.context_dir,
        context=run.agent_context(head_sha=verdict.head_sha),
        prompt=(f"Use the pr-manager skill for branch {run.branch} and issue #{run.issue.number}. Read "
                "/context/verdict.json and /context/issue.json. Call list_open_prs; reuse the open PR or create one "
                f"whose body starts with 'Closes #{run.issue.number}' and summarises the change. Then call "
                "wait_for_ci and merge only after it reports success."),
        tools=service.tools(),
    )
    run.write_context("issue.json", run.issue.model_dump())
    try:
        run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
    finally:
        run.recorder.event("pr", "events", service.events)
    result = service.result()
    run.recorder.event("pr", "result", result.model_dump())
    if service.violations:
        raise StageFailure(FailureKind.PR_VIOLATION, f"PR tools misused: {service.violations}")
    if result.status == "merged":
        if result.merge_sha != remote_sha(run.clone, run.repo.base_branch):
            raise StageFailure(FailureKind.GITHUB, "The recorded merge commit is not the base branch head")
        return result
    last = result.ci_observations[-1] if result.ci_observations else None
    if last == "failure":
        raise CIFailed(f"Required checks {run.repo.required_checks} failed on {verdict.head_sha[:12]}", verdict.head_sha)
    if last == "timeout":
        raise StageFailure(FailureKind.CI_TIMEOUT, f"Required checks did not finish within {limits.ci_timeout_seconds}s")
    raise StageFailure(FailureKind.PR_VIOLATION, f"The PR manager stopped with the PR {result.status}")
