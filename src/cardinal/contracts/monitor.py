"""The monitor role's output: the human-readable part of an issue. Evidence is appended by code."""

from pydantic import BaseModel, Field


class IssueDraft(BaseModel):
    title: str = Field(min_length=8, max_length=120, description="The defect, stated as observed behavior")
    body: str = Field(min_length=40, description="What fails, where in the code, and the observable impact")
