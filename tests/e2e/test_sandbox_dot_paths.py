from cardinal.agents.backends import mounts, under


def make_mounts(tmp_path, *, off_limits, hidden, writable):
    worktree, context, skills = tmp_path / "repo", tmp_path / "context", tmp_path / "skills"
    for directory in (worktree, context, skills):
        directory.mkdir()
    (worktree / "README.md").write_text("public readme\n")
    (worktree / "hidden.txt").write_text("SECRET-GRADER-CONTENT\n")
    return worktree, mounts(worktree, context, skills, off_limits, hidden, repo_writable=writable)


def test_dot_prefixed_repo_paths_respect_hidden_and_off_limits(tmp_path):
    worktree, backend = make_mounts(tmp_path, off_limits=[".github/"], hidden=["hidden.txt"], writable=True)

    plain = backend.read("/repo/README.md")
    assert plain.error is None, plain.error
    assert "public readme" in str(plain)

    leaked = backend.read("/repo/./hidden.txt")
    assert leaked.error is not None and "off limits for this repository" in leaked.error, leaked
    assert "SECRET-GRADER-CONTENT" not in str(leaked)

    written = backend.write("/repo/./.github/x.yml", "on: push\n")
    assert written.error is not None and "off limits for this repository" in written.error, written
    assert not (worktree / ".github" / "x.yml").exists()

    assert under("./hidden.txt", ["hidden.txt"])
    assert under("hidden/./x", ["hidden"])
    assert not under("README.md", ["hidden.txt"])
    assert not under("", ["hidden"])
    assert not under("/", ["hidden"])


def test_dot_segment_writes_under_hidden_directory_are_refused(tmp_path):
    worktree, backend = make_mounts(tmp_path, off_limits=[], hidden=["hidden"], writable=True)

    written = backend.write("/repo/hidden/./x", "graded\n")
    assert written.error is not None and "off limits for this repository" in written.error, written
    assert not (worktree / "hidden" / "x").exists()

    allowed = backend.write("/repo/./notes.md", "fine\n")
    assert allowed.error is None, allowed.error
    assert (worktree / "notes.md").read_text() == "fine\n"
