"""Operator configuration. Unknown keys and missing decisions refuse rather than default open."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cardinal.home import SLUG


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleModels(Strict):
    """One model spec per role, e.g. `openai:gpt-6-sol`. No role inherits another's by accident."""

    orchestrator: str = Field(min_length=1)
    profiler: str = Field(min_length=1)
    coder: str = Field(min_length=1)
    verifier: str = Field(min_length=1)
    pr_manager: str = Field(min_length=1)
    deployer: str = Field(min_length=1)
    monitor: str = Field(min_length=1)


class Limits(Strict):
    # Deliberately generous: middleware adds graph steps per turn (lessons/recursion-limit-tracks-graph-shape.md).
    recursion_limit: int = Field(default=250, ge=50)
    coder_attempts: int = Field(default=3, ge=1, le=6)
    verify_rounds: int = Field(default=2, ge=1, le=4)
    ci_repair_rounds: int = Field(default=1, ge=0, le=2, description="Coder rounds fed a failed required check's log")
    base_sync_rounds: int = Field(default=2, ge=0, le=4, description="Merges of a moved base branch into a run's branch")
    test_timeout_seconds: int = Field(default=1500, ge=30)
    ci_timeout_seconds: int = Field(default=2400, ge=60)
    ci_poll_seconds: int = Field(default=15, ge=1)
    model_timeout_seconds: int = Field(default=300, ge=10)


class Deploy(Strict):
    transport: Literal["ssh", "local"]
    local_directory: str | None = Field(default=None, description="Required for local transport")


class Labels(Strict):
    ready: str = "cardinal:ready"
    working: str = "cardinal:in-progress"
    done: str = "cardinal:done"
    error: str = "cardinal:error"
    needs_human: str = "cardinal:needs-human"
    investigate: str = "cardinal:investigate"

    def state_axis(self) -> list[str]:
        return [self.ready, self.working, self.done, self.error, self.needs_human, self.investigate]


class Logging(Strict):
    level: Literal["debug", "info", "warning", "error", "critical"]
    source_repo: str = Field(pattern=SLUG.pattern, description="Where Cardinal's own defects are filed")


class Ingest(Strict):
    bind: str = Field(pattern=r"^[^:\s]+:\d{1,5}$", description="host:port the ingest endpoint listens on")
    token_env: str = Field(min_length=1, description="Environment variable holding the bearer token apps send")


class Monitor(Strict):
    min_occurrences: int = Field(ge=1, description="Error records a fingerprint needs within the window")
    window_hours: float = Field(gt=0)
    max_issues_per_pass: int = Field(ge=1, le=20, description="Caps an error storm, including the monitor's own")


class Repo(Strict):
    slug: str = Field(pattern=SLUG.pattern)
    remote_url: str | None = Field(default=None, description="Defaults to https://github.com/<slug>")
    base_branch: str = Field(min_length=1)
    test_command: list[str] = Field(min_length=1, description="argv run in the worktree; no shell")
    required_checks: list[str] = Field(description="CI check names that must pass on the exact head; [] means none")
    paths_off_limits: list[str] = Field(description="Path prefixes agents may not change")
    paths_hidden: list[str] = Field(description="Path prefixes agents may not see or change, such as a grader")
    branch_prefix: str = "cardinal"
    labels: Labels = Labels()
    deploy: Deploy | None = None
    retry_after_hours: float | None = Field(default=None, ge=24.0, description="None disables automatic retry")
    max_parallel_runs: int = Field(default=1, ge=1, le=8, description=(
        "Issues the daemon works at once, each in its own process. Planning stays one issue at a time; "
        "every run executes test_command, so size this to the host"))

    @property
    def url(self) -> str:
        return self.remote_url or f"https://github.com/{self.slug}"


class Config(Strict):
    models: RoleModels
    logging: Logging
    ingest: Ingest | None = None
    monitor: Monitor | None = None
    limits: Limits = Limits()
    repos: list[Repo] = Field(min_length=1)

    def repo(self, slug: str | None) -> Repo:
        if slug is None:
            if len(self.repos) != 1:
                raise ValueError("Several repositories are configured; pass --repo owner/name")
            return self.repos[0]
        for repo in self.repos:
            if repo.slug == slug:
                return repo
        raise ValueError(f"Repository {slug} is not configured")
