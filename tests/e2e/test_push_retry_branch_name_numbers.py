import subprocess
from pathlib import Path

import pytest

from cardinal.repo import git as git_module
from cardinal.repo.git import GitError

SHA = "a" * 40
PREVIOUS = "b" * 40

CASES = [
    # (branch, push stderr, expected push count)
    ("cardinal/12-upload-returns-403-for-large-files",
     " ! [remote rejected] HEAD -> cardinal/12-upload-returns-403-for-large-files (failure)", 3),
    ("cardinal/12-get-notes-returns-500-instead-of-400",
     " ! [remote rejected] HEAD -> cardinal/12-get-notes-returns-500-instead-of-400 "
     "(push declined due to repository rule violations)", 1),
    ("cardinal/7-fix-thing",
     "error: RPC failed; HTTP 502 curl 22 The requested URL returned error: 502", 3),
    ("cardinal/7-fix-thing",
     "remote: Permission to o/r.git denied\nfatal: unable to access 'https://github.com/o/r.git/': "
     "The requested URL returned error: 403", 1),
]


def _push_count(monkeypatch, branch, stderr):
    pushes = []

    def fake_run(argv, **kwargs):
        args = argv[len(["git", *git_module.IDENTITY]):]
        if args[:2] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, stdout=SHA + "\n", stderr="")
        if args[0] == "ls-remote":
            return subprocess.CompletedProcess(argv, 0, stdout=f"{PREVIOUS}\trefs/heads/{branch}\n", stderr="")
        if args[0] == "push":
            pushes.append(args)
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)
        raise AssertionError(f"unexpected git call {args}")

    monkeypatch.setattr(git_module.subprocess, "run", fake_run)
    monkeypatch.setattr(git_module.time, "sleep", lambda seconds: None)
    with pytest.raises(GitError) as raised:
        git_module._publish(Path("/nonexistent-worktree"), branch)
    return len(pushes), str(raised.value)


def test_push_retry_decision_ignores_status_numbers_in_branch_name(monkeypatch):
    failures = []
    for branch, stderr, expected in CASES:
        count, message = _push_count(monkeypatch, branch, stderr)
        if count != expected or f"failed after {expected} attempt(s)" not in message:
            failures.append(f"{stderr!r}: pushed {count} time(s), expected {expected}; error: {message}")
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("branch", [
    "cardinal/7-fix-thing",
    "cardinal/1403-fix-thing",
    "cardinal/12-http-503-from-error-403-page",
])
@pytest.mark.parametrize("template, expected", [
    (" ! [remote rejected] HEAD -> {branch} (failure)", 3),
    (" ! [remote rejected] HEAD -> {branch} (push declined due to repository rule violations)", 1),
])
def test_same_push_failure_gets_same_decision_whatever_the_branch(monkeypatch, branch, template, expected):
    count, message = _push_count(monkeypatch, branch, template.format(branch=branch))
    assert count == expected, message
    assert f"failed after {expected} attempt(s)" in message
