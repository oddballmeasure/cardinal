"""Contracts the grading harness owns: acceptance evidence and cleanup requests."""

from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    repository: str
    number: int
    title: str
    body: str


class CheckEvidence(BaseModel):
    selector: str
    exit_code: int
    passed_tests: int
    stdout: str
    stderr: str


class CleanupIssue(BaseModel):
    action: Literal["reopen", "create"]
    number: int | None = None
    title: str | None = None
    body: str | None = None
    labels: list[str] = []


class CleanupRequest(BaseModel):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    remote_url: str
    base_branch: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    managed_branches: list[str]
    managed_pr_numbers: list[int]
    issue_action: Literal["reopen", "create"]
    issue_number: int | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    issue_resets: list[CleanupIssue] = []


class CleanupResult(BaseModel):
    repository: str
    base_branch: str
    base_sha: str
    previous_sha: str
    remote_sha: str
    issue_number: int
    issue_numbers: list[int] = []
    closed_pr_numbers: list[int]
    historical_merged_pr_numbers: list[int]
    deleted_branches: list[str]
    clean: bool
