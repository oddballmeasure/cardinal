"""The verifier's runtime check of requirement IDs names what is wrong with the assessment."""

from types import SimpleNamespace

from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision, Requirement, Ticket
from cardinal.contracts.verdict import VerifierAssessment
from cardinal.roles import verifier

HEAD = "a" * 40


def fake_run():
    events = []
    return SimpleNamespace(
        worktree="/nonexistent", branch="cardinal/issue-7", context_dir="/nonexistent/context",
        issue=SimpleNamespace(model_dump=lambda: {}),
        write_context=lambda name, data: None,
        agent_context=lambda **kwargs: kwargs,
        repo=SimpleNamespace(paths_hidden=[], paths_off_limits=[], test_command=["true"]),
        config=SimpleNamespace(models=SimpleNamespace(verifier="openai:unused"), ceiling=None,
                               limits=SimpleNamespace(recursion_limit=10, model_timeout_seconds=10,
                                                      test_timeout_seconds=10)),
        recorder=SimpleNamespace(event=lambda *args: events.append(args)),
    )


def decision():
    return IntakeDecision(
        issue_number=7, kind="bug", reason="r", size="s", risks=[],
        requirements=[Requirement(id="R1", text="does the thing")],
        tickets=[Ticket(id="T1", source_issue=7, title="t", task="t", acceptance_criteria=["a"],
                        covers=["R1"], depends_on=[], target_files=["app.py"])])


def verdict_for(monkeypatch, requirements):
    monkeypatch.setattr(verifier, "head", lambda worktree: HEAD)
    monkeypatch.setattr(verifier, "changed_files", lambda worktree, base: ["app.py", "tests/test_app.py"])
    monkeypatch.setattr(verifier, "patch", lambda worktree, base: "")
    approved = all(requirements.values())
    assessment = VerifierAssessment(head_sha=HEAD, approved=approved, requirements=requirements,
                                    tests_are_e2e=True, findings=[])
    monkeypatch.setattr(verifier, "run_agent", lambda *args, **kwargs: assessment)
    evidence = CommandEvidence(command=["true"], exit_code=0, stdout_tail="", stderr_tail="")
    return verifier.verify(fake_run(), decision(), "b" * 40, evidence)


def test_verifier_names_unknown_requirement_ids_instead_of_an_empty_missing_list(monkeypatch):
    verdict = verdict_for(monkeypatch, {"R1": True, "R2": True})
    assert not verdict.approved
    assert "Requirements not shown to be delivered: []" not in verdict.findings, verdict.findings
    assert any("R2" in finding for finding in verdict.findings), verdict.findings

    undelivered = verdict_for(monkeypatch, {"R1": False})
    assert "Requirements not shown to be delivered: ['R1']" in undelivered.findings, undelivered.findings
