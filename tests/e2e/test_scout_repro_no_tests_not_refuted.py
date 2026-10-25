"""A focused repro run that ran no tests but exits 0 is not a refutation (issue #62)."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from cardinal.contracts.scout import ReproResult
from cardinal.repo.git import git
from cardinal.roles.scout_repro import confirm_focused

TEST_PATH = "tests/test_bug.py"
TEST_NAME = "test_bug_reproduces"


def make_run(timeout: int = 30) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(limits=SimpleNamespace(test_timeout_seconds=timeout)))


def make_worktree(tmp_path: Path) -> tuple[Path, str]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git(worktree, "init", "-q")
    (worktree / "README").write_text("base\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "base")
    base_sha = git(worktree, "rev-parse", "HEAD").strip()
    (worktree / "tests").mkdir()
    (worktree / TEST_PATH).write_text("def test_bug_reproduces():\n    assert False\n")
    return worktree, base_sha


def result() -> ReproResult:
    return ReproResult(test_path=TEST_PATH, test_name=TEST_NAME, note="shows the bug")


def focused(script: str) -> list[str]:
    # argv[1:] carry the placeholders, so focused_command has something to substitute.
    return [sys.executable, "-c", script, "{test_path}", "{test_name}"]


def test_exit_0_with_no_tests_found_is_not_a_refutation(tmp_path):
    worktree, base_sha = make_worktree(tmp_path)
    repro = confirm_focused(make_run(), worktree, base_sha, result(),
                            focused("print('No tests found, exiting with code 0')"))
    assert repro.ok is False
    assert repro.refuted is False
    assert "the focused run ran no tests" in repro.reason
    assert TEST_NAME in repro.reason


def test_exit_0_with_ordinary_output_is_a_refutation(tmp_path):
    worktree, base_sha = make_worktree(tmp_path)
    repro = confirm_focused(make_run(), worktree, base_sha, result(),
                            focused("import sys; print('1 passed:', sys.argv[2])"))
    assert repro.ok is False
    assert repro.refuted is True
    assert "passes on the base branch" in repro.reason


def test_timed_out_run_reports_timeout(tmp_path):
    worktree, base_sha = make_worktree(tmp_path)
    repro = confirm_focused(make_run(timeout=1), worktree, base_sha, result(),
                            focused("import time; print('No tests found', flush=True); time.sleep(30)"))
    assert repro.ok is False
    assert repro.refuted is False
    assert "timed out" in repro.reason


def test_failing_run_that_names_the_test_confirms_the_bug(tmp_path):
    worktree, base_sha = make_worktree(tmp_path)
    repro = confirm_focused(make_run(), worktree, base_sha, result(),
                            focused("import sys; print('FAILED', sys.argv[1] + '::' + sys.argv[2]); sys.exit(1)"))
    assert repro.ok is True
    assert repro.refuted is False
    assert TEST_PATH in repro.diff
