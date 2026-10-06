"""Judge the whole branch against the whole issue, bound to one exact commit."""

import re

from langchain_core.tools import tool

from cardinal.agents.backends import under
from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision
from cardinal.contracts.verdict import Verdict, VerifierAssessment
from cardinal.graph.context import Run
from cardinal.repo.git import changed_files, head, patch
from cardinal.roles.tests import run_repo_tests, summary

TEST_PATH = re.compile(r"(^|/)(tests?|e2e|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH.search(path))


def verify(run: Run, decision: IntakeDecision, base_sha: str, head_evidence: CommandEvidence | None) -> Verdict:
    head_sha = head(run.worktree)
    changed = changed_files(run.worktree, base_sha)
    run.write_context("issue.json", run.issue.model_dump())
    run.write_context("decision.json", {"requirements": [item.model_dump() for item in decision.requirements],
                                        "tickets": [ticket.model_dump() for ticket in decision.tickets]})
    run.write_context("revision.json", {"branch": run.branch, "head_sha": head_sha, "base_sha": base_sha,
                                        "changed_files": changed})
    run.write_context("patch.diff", patch(run.worktree, base_sha))

    @tool("run_repo_tests")
    def run_repo_tests_tool() -> str:
        """Run the repository's own test command against the branch head."""
        return summary(run_repo_tests(run.worktree, run.repo.test_command, run.config.limits.test_timeout_seconds))

    call = AgentCall(
        stage="verifier", model_spec=run.config.models.verifier, context_dir=run.context_dir,
        context=run.agent_context(head_sha=head_sha, requirement_ids=[item.id for item in decision.requirements]),
        worktree=run.worktree, repo_writable=False, hidden=run.repo.paths_hidden,
        prompt=("Use the verifier skill. Read /context/issue.json, /context/decision.json, /context/revision.json "
                "and /context/patch.diff, and inspect the changed code and tests under /repo/ (read-only). "
                "Return a VerifierAssessment for the full issue at the head_sha in revision.json."),
        tools=[run_repo_tests_tool], schema=VerifierAssessment,
    )
    limits = run.config.limits
    assessment: VerifierAssessment = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds)
    if head(run.worktree) != head_sha:
        raise RuntimeError("The branch head moved during verification")
    if head_evidence is None:
        head_evidence = run_repo_tests(run.worktree, run.repo.test_command, limits.test_timeout_seconds)
    ids = {item.id for item in decision.requirements}
    touched = sorted(path for path in changed if under(path, run.repo.paths_off_limits + run.repo.paths_hidden))
    tests_changed = any(is_test_path(path) for path in changed)
    findings = list(assessment.findings)
    if not assessment.approved and not findings:
        findings.append("The verifier did not approve the change")
    if assessment.head_sha != head_sha:
        findings.append(f"The assessment names head {assessment.head_sha}, not {head_sha}")
    if set(assessment.requirements) != ids or not all(assessment.requirements.values()):
        missing = sorted(ids - {key for key, value in assessment.requirements.items() if value})
        findings.append(f"Requirements not shown to be delivered: {missing}")
    if not assessment.tests_are_e2e:
        findings.append("The repository tests added for this change do not exercise the application boundary")
    if not tests_changed:
        findings.append("No repository test changed with the code")
    if touched:
        findings.append(f"Off-limits paths changed: {touched}")
    if not head_evidence.passed:
        findings.append("The repository's test command fails at the branch head:\n" + summary(head_evidence)[-3000:])
    verdict = Verdict(branch=run.branch, head_sha=head_sha, approved=not findings, requirements=assessment.requirements,
                      tests_are_e2e=assessment.tests_are_e2e, repo_tests_passed=head_evidence.passed,
                      test_files_changed=tests_changed, off_limits_touched=touched, findings=findings)
    run.recorder.event("verify", "verdict", verdict.model_dump())
    return verdict
