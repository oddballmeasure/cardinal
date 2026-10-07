"""Why a run stopped, as a type the daemon can route on (lessons/classify-failures-from-counters.md)."""

import logging
from enum import StrEnum


class FailureKind(StrEnum):
    PROVIDER = "provider"                    # model API outage, auth, rate limit, context overflow
    TURNS = "turns"                          # agent hit its recursion limit before answering
    NO_ANSWER = "no_answer"                  # agent stopped without its structured result
    INVALID_OUTPUT = "invalid_output"        # structured result failed validation
    TESTS_FAILED = "tests_failed"            # coder attempts exhausted with repo tests red
    NO_CHANGE = "no_change"                  # coder attempts exhausted without editing anything
    VERIFICATION = "verification"            # verifier rounds exhausted without approval
    CI_FAILED = "ci_failed"
    CI_TIMEOUT = "ci_timeout"
    BASE_CONFLICT = "base_conflict"          # the base branch moved and still conflicts after base_sync_rounds
    PR_VIOLATION = "pr_violation"            # PR manager misused a tool or stopped before merging
    DEPLOY = "deploy"                        # deployment script failed or never became healthy
    GIT = "git"
    GITHUB = "github"
    CRASH = "crash"                          # a defect in Cardinal itself

    @property
    def level(self) -> int:
        """Defects in Cardinal or its tools are errors; outcomes of honest work are warnings."""
        outcomes = {FailureKind.TESTS_FAILED, FailureKind.NO_CHANGE, FailureKind.VERIFICATION,
                    FailureKind.CI_FAILED, FailureKind.CI_TIMEOUT, FailureKind.BASE_CONFLICT,
                    FailureKind.PROVIDER}
        return logging.WARNING if self in outcomes else logging.ERROR

    @property
    def retryable(self) -> bool:
        return self not in {FailureKind.CRASH, FailureKind.DEPLOY}


class StageFailure(Exception):
    def __init__(self, kind: FailureKind, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
