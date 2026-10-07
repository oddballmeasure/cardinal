"""What the checkpointer keeps between nodes. Plain JSON-able values only."""

from typing import TypedDict


class RunState(TypedDict, total=False):
    status: str                   # running | awaiting_human | rejected | done | failed
    failure: dict | None          # {"kind": FailureKind, "detail": str}
    base_sha: str
    profile_revision: str
    decision: dict                # IntakeDecision
    tickets: list[dict]           # Tickets in dependency order
    question: str                 # what the orchestrator asked a person
    human_answer: str | None
    repair: list[str] | None      # verifier findings to resolve in the next implement round
    ci_repair: list[str] | None   # failed required-check logs to resolve in the next implement round
    ci_round: int                 # CI repair rounds used so far
    verify_round: int
    sync: dict | None             # {"conflicts": [paths]} or {"tests": summary} after merging a moved base
    sync_round: int               # base merges used so far
    resync: bool                  # GitHub reported the PR conflicting; merge the base again
    head_sha: str
    head_tests: dict              # CommandEvidence observed at head_sha
    verdict: dict
    pr: dict
    merge_sha: str
    deployment: dict
