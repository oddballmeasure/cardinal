"""Operator configuration. Unknown keys and missing decisions refuse rather than default open."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    # Only the scout uses these, so they are required only when [scout] is present (Config checks).
    scout: str | None = Field(default=None, min_length=1, description="Plans, surveys and writes repro tests")
    scout_reviewer: str | None = Field(default=None, min_length=1, description="Re-derives each proposal from the code")


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
    proposed: str = "cardinal:proposed"  # the scout's proposals; off the state axis, so the daemon ignores it

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


class Scout(Strict):
    """`cardinal scout`: find, review and propose work. Every key is a decision; none defaults."""

    autonomy: Literal["propose", "auto"] = Field(description="auto lets a category with a track record file ready")
    categories: list[Literal["bug", "feature"]] = Field(min_length=1)
    areas_per_pass: int = Field(ge=1, le=6, description="Areas surveyed per pass; the oldest-surveyed go first")
    cooldown_days: float = Field(ge=0, description="An area surveyed this recently is skipped")
    max_proposals_per_pass: int = Field(ge=1, le=10, description="Issues filed per pass; the rest wait for the next")
    max_files: int = Field(ge=1, le=10, description="Files a proposal's fix may change")
    repro: bool = Field(description="Bugs get a test that must fail on the base branch")
    auto_min_decided: int = Field(ge=1, description="Proposals of a category approved or rejected in 60 days")
    auto_min_approval: float = Field(ge=0, le=1)
    auto_min_done: float = Field(ge=0, le=1, description="Share of a category's landed issues that ended done")


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
    scout: Scout | None = None
    limits: Limits = Limits()
    repos: list[Repo] = Field(min_length=1)

    @model_validator(mode="after")
    def scout_models(self) -> "Config":
        missing = [role for role in ("scout", "scout_reviewer") if getattr(self.models, role) is None]
        if self.scout is not None and missing:
            raise ValueError(f"[scout] needs [models] {', '.join(missing)}")
        return self

    def repo(self, slug: str | None) -> Repo:
        if slug is None:
            if len(self.repos) != 1:
                raise ValueError("Several repositories are configured; pass --repo owner/name")
            return self.repos[0]
        for repo in self.repos:
            if repo.slug == slug:
                return repo
        raise ValueError(f"Repository {slug} is not configured")
