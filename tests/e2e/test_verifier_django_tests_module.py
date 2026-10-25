"""A Django app's tests.py counts as a repository test when the verifier judges a branch."""

from types import SimpleNamespace

from cardinal.contracts.evidence import CommandEvidence
from cardinal.contracts.intake import IntakeDecision, Requirement, Ticket
from cardinal.contracts.verdict import VerifierAssessment
from cardinal.roles import verifier

HEAD = "a" * 40


def fake_run():
    return SimpleNamespace(
        worktree="/nonexistent", branch="cardinal/issue-7", context_dir="/nonexistent/context",
        issue=SimpleNamespace(model_dump=lambda: {}),
        write_context=lambda name, data: None,
        agent_context=lambda **kwargs: kwargs,
        repo=SimpleNamespace(paths_hidden=[], paths_off_limits=[], test_command=["true"]),
        config=SimpleNamespace(models=SimpleNamespace(verifier="openai:unused"), ceiling=None,
                               limits=SimpleNamespace(recursion_limit=10, model_timeout_seconds=10,
                                                      test_timeout_seconds=10)),
        recorder=SimpleNamespace(event=lambda *args: None),
    )


def decision():
    return IntakeDecision(
        issue_number=7, kind="bug", reason="r", size="s", risks=[],
        requirements=[Requirement(id="R1", text="does the thing")],
        tickets=[Ticket(id="T1", source_issue=7, title="t", task="t", acceptance_criteria=["a"],
                        covers=["R1"], depends_on=[], target_files=["app/views.py"])])


def test_tests_py_is_a_test_path_but_lookalike_modules_are_not():
    assert verifier.is_test_path("app/tests.py")
    assert verifier.is_test_path("tests.py")
    assert not verifier.is_test_path("app/contests.py")
    assert not verifier.is_test_path("app/views.py")
    assert not verifier.is_test_path("src/cardinal/roles/testsuite.py")
    assert not verifier.is_test_path("app/tests.pyc")
    for existing in ("tests/e2e/x.py", "test_x.py", "x_test.py", "x.spec.ts"):
        assert verifier.is_test_path(existing), existing


def test_verifier_counts_django_app_tests_py_as_a_test_change(monkeypatch):
    monkeypatch.setattr(verifier, "head", lambda worktree: HEAD)
    monkeypatch.setattr(verifier, "changed_files", lambda worktree, base: ["app/views.py", "app/tests.py"])
    monkeypatch.setattr(verifier, "patch", lambda worktree, base: "")
    assessment = VerifierAssessment(head_sha=HEAD, approved=True, requirements={"R1": True},
                                    tests_are_e2e=True, findings=[])
    monkeypatch.setattr(verifier, "run_agent", lambda *args, **kwargs: assessment)
    evidence = CommandEvidence(command=["true"], exit_code=0, stdout_tail="", stderr_tail="")
    verdict = verifier.verify(fake_run(), decision(), "b" * 40, evidence)
    assert "No repository test changed with the code" not in verdict.findings, verdict.findings
    assert verdict.test_files_changed
    assert verdict.approved, verdict.findings
