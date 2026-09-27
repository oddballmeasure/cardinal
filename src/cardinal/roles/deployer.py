"""Deploy the merged revision when the repository's configuration asks for it."""

from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.deploy import DeploymentResult
from cardinal.deploy.committed import committed_deployment
from cardinal.deploy.hosts import host_for
from cardinal.deploy.service import DeploymentService
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.repo.git import git


def deploy(run: Run, merge_sha: str) -> DeploymentResult:
    settings = run.repo.deploy
    if settings is None:
        raise ValueError("deploy called for a repository without deploy configuration")
    git(run.clone, "fetch", "-q", "origin", run.repo.base_branch, timeout=600)
    try:
        script_path, script, config_bytes, config = committed_deployment(run.clone, merge_sha)
    except ValueError as exc:
        raise StageFailure(FailureKind.DEPLOY, str(exc)) from exc
    run.write_context("deployment/deploy.sh", script.decode(errors="replace"))
    run.write_context("deployment/deploy.json", config_bytes.decode(errors="replace"))
    host = host_for(config, settings, run.home.run_dir(run.run_id) / "deploy-host")
    service = DeploymentService(merge_sha, script_path, script, config, host)
    call = AgentCall(
        stage="deployer", model_spec=run.config.models.deployer, context_dir=run.context_dir,
        context=run.agent_context(merge_sha=merge_sha),
        prompt=("Use the deployment-manager skill. Read /context/deployment/deploy.json and "
                "/context/deployment/deploy.sh from the merged revision. Run the deployment once, then poll "
                "health until it is healthy or timed out. Stop if the script fails."),
        tools=service.tools(),
    )
    limits = run.config.limits
    run_agent(call, run.recorder, max(limits.recursion_limit, service.max_polls * 6), limits.model_timeout_seconds)
    run.recorder.event("deploy", "events", service.events)
    if service.violations:
        raise StageFailure(FailureKind.DEPLOY, f"Deployment tools misused: {service.violations}")
    try:
        result = service.result()
    except ValueError as exc:
        raise StageFailure(FailureKind.DEPLOY, str(exc)) from exc
    run.recorder.event("deploy", "result", result.model_dump())
    if result.status != "healthy":
        raise StageFailure(FailureKind.DEPLOY, f"Deployment of {merge_sha[:12]} ended {result.status}")
    return result
