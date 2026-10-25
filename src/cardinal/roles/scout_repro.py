"""Turn a confirmed bug into one repository test that fails on the base branch. The agent writes the
test in a scratch worktree; code then checks that only test files changed, that the suite fails,
and that the new test is what fails, against a base known to be green. A repository with a
focused_test_command runs only the new test, both for the agent and for that confirming run."""

from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import tool

from cardinal.agents.backends import vendored
from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.scout import Proposal, ReproResult
from cardinal.graph.context import ScoutRun
from cardinal.repo.git import changed_files, detached_worktree, patch, remove_worktree
from cardinal.roles.tests import focused_command, ran_no_tests, run_repo_tests, summary
from cardinal.roles.verifier import is_test_path


@dataclass
class Repro:
    ok: bool
    refuted: bool  # the test passes on base: the bug did not reproduce
    reason: str
    diff: str = ""


def reproduce(run: ScoutRun, proposal: Proposal, base_sha: str, base_green: bool, index: int) -> Repro:
    if not base_green:
        return Repro(False, False, "the test command fails on the base branch, so no new test can show the bug")
    repo, limits = run.repo, run.config.limits
    worktree = run.worktree.parent / f"{run.run_id}-repro-{index}"
    detached_worktree(run.clone, worktree, base_sha)
    try:
        run.write_context("proposal.json", proposal.model_dump())

        focused = repo.focused_test_command
        if focused:
            @tool("run_repo_tests")
            def run_repo_tests_tool(test_path: str, test_name: str) -> str:
                """Run only your new test in your worktree. test_path is the test file's repo-relative path;
                test_name is the test's name as the test runner reports it."""
                command = focused_command(focused, test_path, test_name)
                return summary(run_repo_tests(worktree, command, limits.test_timeout_seconds))
        else:
            @tool("run_repo_tests")
            def run_repo_tests_tool() -> str:
                """Run the repository's own test command in your worktree."""
                return summary(run_repo_tests(worktree, repo.test_command, limits.test_timeout_seconds))

        call = AgentCall(
            stage="scout_repro", model_spec=run.config.models.scout, context_dir=run.context_dir,
            context=run.agent_context(proposal=proposal.model_dump(), worktree=str(worktree)), worktree=worktree,
            repo_writable=True, off_limits=repo.paths_off_limits, hidden=repo.paths_hidden,
            prompt=("Use the scout-repro skill. Write one test under /repo/ that fails because of the bug in "
                    "/context/proposal.json, run it with run_repo_tests, and return one ReproResult."),
            tools=[run_repo_tests_tool], schema=ReproResult,
        )
        result: ReproResult = run_agent(call, run.recorder, limits.recursion_limit, limits.model_timeout_seconds,
                                         ceiling=run.config.ceiling)
        changed = [path for path in changed_files(worktree, base_sha) if not vendored(path)]
        if not changed:
            return Repro(False, False, "the repro wrote no test")
        others = [path for path in changed if not is_test_path(path)]
        if others:
            return Repro(False, False, f"the repro changed files that are not tests: {others}")
        if focused:
            return confirm_focused(run, worktree, base_sha, result, focused)
        evidence = run_repo_tests(worktree, repo.test_command, limits.test_timeout_seconds)
        if evidence.passed:
            return Repro(False, True, f"{result.test_name} passes on the base branch: the bug did not reproduce")
        if evidence.timed_out or result.test_name not in summary(evidence):
            return Repro(False, False, f"the suite failed, but not visibly on {result.test_name}")
        return Repro(True, False, f"{result.test_name} fails on the base branch", patch(worktree, base_sha))
    finally:
        remove_worktree(run.clone, worktree)


def confirm_focused(run: ScoutRun, worktree: Path, base_sha: str, result: ReproResult, focused: list[str]) -> Repro:
    """Run only the new test. It confirms the bug only by failing visibly, by name, within the time limit."""
    command = focused_command(focused, result.test_path, result.test_name)
    evidence = run_repo_tests(worktree, command, run.config.limits.test_timeout_seconds)
    if evidence.timed_out:
        return Repro(False, False, f"the focused run of {result.test_name} timed out")
    # Some runners print "no tests found" and still exit 0; a run that ran nothing refutes nothing.
    if ran_no_tests(evidence):
        return Repro(False, False, f"the focused run ran no tests, so it does not show {result.test_name} failing")
    if evidence.passed:
        return Repro(False, True, f"{result.test_name} passes on the base branch: the bug did not reproduce")
    # The command itself names the test, so only the run's output counts as naming it.
    if result.test_name not in evidence.stdout_tail + evidence.stderr_tail:
        return Repro(False, False, f"the focused run failed, but not visibly on {result.test_name}")
    return Repro(True, False, f"{result.test_name} fails on the base branch", patch(worktree, base_sha))
