"""The orchestrator's single output: whether to take the issue, and if so, how to split it."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

WORKABLE = {"feature", "bug", "chore"}


class Requirement(BaseModel):
    id: str = Field(min_length=1, description="Short stable ID, e.g. R1; reuse IDs the issue already states")
    text: str = Field(min_length=1, description="One observable behavior the issue asks for")


class Ticket(BaseModel):
    id: str = Field(min_length=1)
    source_issue: int
    title: str = Field(min_length=1)
    task: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    covers: list[str] = Field(min_length=1, description="Requirement IDs this ticket delivers")
    depends_on: list[str]
    target_files: list[str] = Field(min_length=1, description="Profiled paths the coder should start from")


class IntakeDecision(BaseModel):
    issue_number: int
    kind: Literal["feature", "bug", "chore", "question", "needs_human", "reject"]
    reason: str = Field(min_length=1, description="Why this kind; for needs_human, the exact question for a person")
    size: Literal["xs", "s", "m", "l", "xl"]
    risks: list[str] = Field(description="Risks from the orchestrator skill's list that apply; [] if none")
    requirements: list[Requirement] = Field(description="Every requested behavior; [] only when not workable")
    tickets: list[Ticket] = Field(max_length=4, description="1-4 tickets when workable; [] otherwise")

    @model_validator(mode="after")
    def tickets_match_kind(self) -> "IntakeDecision":
        workable = self.kind in WORKABLE
        if workable and (not self.tickets or not self.requirements):
            raise ValueError(f"A {self.kind} decision needs requirements and 1-4 tickets")
        if not workable and self.tickets:
            raise ValueError(f"A {self.kind} decision must not carry tickets")
        return self

    @property
    def workable(self) -> bool:
        return self.kind in WORKABLE
