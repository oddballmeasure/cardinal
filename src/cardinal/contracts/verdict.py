from pydantic import BaseModel, Field


class VerifierAssessment(BaseModel):
    """What the verifier agent concludes. The runtime re-derives every fact it can check itself."""

    head_sha: str
    approved: bool
    requirements: dict[str, bool] = Field(description="Every requirement ID mapped to whether the change delivers it")
    tests_are_e2e: bool = Field(description="New repository tests exercise the running application's boundary")
    findings: list[str] = Field(description="Unresolved blockers only; [] when approving")


class Verdict(BaseModel):
    """The runtime's verdict, bound to one exact commit."""

    branch: str
    head_sha: str
    approved: bool
    requirements: dict[str, bool]
    tests_are_e2e: bool
    repo_tests_passed: bool
    test_files_changed: bool
    off_limits_touched: list[str]
    findings: list[str]
