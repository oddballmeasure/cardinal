"""What the scout's roles return. Code re-checks every claim it can (scout/checks.py)."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Category = Literal["bug", "feature"]


class Area(BaseModel):
    name: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1, max_length=8, description="Tracked files or directories, repo-relative")
    focus: str = Field(min_length=1, description="What to look for there")


class SurveyPlan(BaseModel):
    areas: list[Area] = Field(min_length=1, max_length=6)


class Evidence(BaseModel):
    path: str = Field(description="Repo-relative file path")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    quote: str = Field(min_length=8, description="Text copied exactly from those lines")


class Proposal(BaseModel):
    category: Category
    title: str = Field(min_length=8, max_length=100, description="The observable behaviour, not the fix")
    problem: str = Field(min_length=40, description="What happens today, with exact values")
    evidence: list[Evidence] = Field(min_length=1, max_length=5)
    expected: str = Field(min_length=10, description="What should happen instead")
    acceptance: list[str] = Field(min_length=1, max_length=6, description="Each names an exact value: a status, count, message or label")
    files: list[str] = Field(min_length=1, description="Existing files a fix would change")


class ScoutFindings(BaseModel):
    proposals: list[Proposal] = Field(max_length=5, description="[] when the area has nothing worth filing")


class ProposalReview(BaseModel):
    verdict: Literal["confirmed", "wrong", "product_decision", "too_big", "duplicate"]
    reason: str = Field(min_length=10)
    evidence: list[Evidence] = Field(description="The evidence you re-derived from the code; required when confirmed")

    @model_validator(mode="after")
    def confirmed_has_evidence(self) -> "ProposalReview":
        if self.verdict == "confirmed" and not self.evidence:
            raise ValueError("a confirmed review must carry the evidence it re-derived")
        return self


class ReproResult(BaseModel):
    test_path: str = Field(description="Repo-relative path of the test file you wrote")
    test_name: str = Field(min_length=3, description="The new test's name as the test runner reports it")
    note: str = Field(description="How the test shows the defect")
