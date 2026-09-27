from typing import Literal

from pydantic import BaseModel


class PRResult(BaseModel):
    number: int | None
    url: str | None
    branch: str
    head_sha: str
    created: bool
    status: Literal["merged", "blocked_ci", "not_opened"]
    ci_observations: list[str]
    merge_sha: str | None
