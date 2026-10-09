"""Git operations on the shared clone and per-issue worktrees. Every call raises on failure.

Parallel runs share the clone's refs and worktree list, so every call that writes them holds the
clone's `git` lock (cardinal/locks.py). Work inside one worktree needs no lock."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import cardinal
from cardinal.locks import lock

PRODUCT_ROOT = Path(cardinal.__file__).resolve().parents[2]
IDENTITY = ["-c", "user.name=Cardinal", "-c", "user.email=cardinal@users.noreply.github.com"]
# Push and fetch with the gh CLI's credential, whatever helper the host has configured.
CREDENTIALS = {
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
    "GIT_TERMINAL_PROMPT": "0",
}


class GitError(RuntimeError):
    pass


def run_git(repo: Path, *args: str, timeout: int = 120, codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *IDENTITY, *args], cwd=repo, text=True, capture_output=True, check=False,
        timeout=timeout, env={**os.environ, **CREDENTIALS},
    )
    if result.returncode not in codes:
        raise GitError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    return run_git(repo, *args, timeout=timeout).stdout


def guard_target(url: str) -> None:
    """Cardinal works on clones of other repositories, never on its own checkout."""
    local = Path(url).expanduser()
    if local.exists() and local.resolve() == PRODUCT_ROOT:
        raise GitError(f"Refusing to operate on Cardinal's own checkout at {PRODUCT_ROOT}")


def ensure_clone(clone: Path, url: str, base: str) -> Path:
    guard_target(url)
    with lock(clone, "git"):
        return _ensure_clone(clone, url, base)


def _ensure_clone(clone: Path, url: str, base: str) -> Path:
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        git(clone.parent, "clone", "-q", "--no-checkout", url, str(clone), timeout=600)
    if git(clone, "remote", "get-url", "origin").strip() != url:
        raise GitError(f"Clone at {clone} does not point at {url}")
    git(clone, "fetch", "-q", "--prune", "origin", timeout=600)
    git(clone, "rev-parse", "--verify", f"refs/remotes/origin/{base}")
    return clone


def remote_sha(clone: Path, branch: str) -> str | None:
    out = git(clone, "ls-remote", "origin", f"refs/heads/{branch}").split()
    return out[0] if out else None


def create_worktree(clone: Path, path: Path, branch: str, base: str) -> str:
    """Cut the branch from a freshly fetched base. Returns the base commit it started from."""
    with lock(clone, "git"):
        base_sha = fetch_base(clone, clone, base)
        for stale in worktrees_on(clone, branch):  # a failed earlier run of this issue keeps its worktree
            remove_worktree(clone, stale)
        path.parent.mkdir(parents=True, exist_ok=True)
        git(clone, "worktree", "add", "-q", "-B", branch, str(path), base_sha)
    return base_sha


def detached_worktree(clone: Path, path: Path, sha: str) -> None:
    """A worktree at one commit with no branch, for work that is never pushed (the scout)."""
    with lock(clone, "git"):
        path.parent.mkdir(parents=True, exist_ok=True)
        git(clone, "worktree", "add", "-q", "--detach", str(path), sha)


def fetch_base(clone: Path, repo: Path, base: str) -> str:
    """Fetch the base branch from `repo` (the clone or one of its worktrees); returns its newest commit."""
    with lock(clone, "git"):
        git(repo, "fetch", "-q", "origin", base, timeout=600)
        return git(repo, "rev-parse", f"refs/remotes/origin/{base}").strip()


def worktrees_on(clone: Path, branch: str) -> list[Path]:
    found, current = [], None
    for line in git(clone, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and current is not None:
            found.append(current)
    return found


def remove_worktree(clone: Path, path: Path) -> None:
    with lock(clone, "git"):
        if path.exists():
            git(clone, "worktree", "remove", "--force", str(path))
        git(clone, "worktree", "prune")


def head(worktree: Path) -> str:
    return git(worktree, "rev-parse", "HEAD").strip()


def commit_all(worktree: Path, message: str) -> str:
    """Commit everything. During a merge this commit completes it, so it refuses while markers remain."""
    left = unresolved(worktree)
    if left:
        raise GitError(f"Refusing to commit: conflict markers remain in {left}")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", message)
    return head(worktree)


def dirty(worktree: Path) -> bool:
    return bool(git(worktree, "status", "--porcelain").strip())


def changed_files(worktree: Path, base_sha: str) -> list[str]:
    git(worktree, "add", "-A", "-N")
    return [line for line in git(worktree, "diff", "--name-only", base_sha).splitlines() if line]


def patch(worktree: Path, base_sha: str) -> str:
    git(worktree, "add", "-A", "-N")
    return git(worktree, "diff", "--binary", base_sha)


def publish(clone: Path, worktree: Path, branch: str) -> str:
    """Push the branch. Overwrite a previous managed push only under an exact lease."""
    with lock(clone, "git"):  # the push updates the clone's remote-tracking refs
        return _publish(worktree, branch)


def _publish(worktree: Path, branch: str) -> str:
    sha = head(worktree)
    previous = remote_sha(worktree, branch)
    if previous is None:
        git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", timeout=300)
    elif previous != sha:
        git(worktree, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{previous}",
            "origin", f"HEAD:refs/heads/{branch}", timeout=300)
    if remote_sha(worktree, branch) != sha:
        raise GitError(f"Remote {branch} does not hold {sha} after push")
    return sha


def fingerprint(worktree: Path) -> str:
    """Identifies the exact working-tree content, committed or not."""
    git(worktree, "add", "-A", "-N")
    digest = hashlib.sha256(head(worktree).encode())
    digest.update(git(worktree, "diff", "--binary", "HEAD").encode())
    return digest.hexdigest()


def ticket_commits(worktree: Path, base_sha: str) -> dict[str, str]:
    """Tickets already committed on this branch, from their `Cardinal-Ticket:` trailers."""
    out = git(worktree, "log", "--format=%H%x00%(trailers:key=Cardinal-Ticket,valueonly,separator=)", f"{base_sha}..HEAD")
    commits: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ticket = line.partition("\0")
        if ticket.strip():
            commits.setdefault(ticket.strip(), sha)
    return commits


def reset_to(worktree: Path, sha: str) -> None:
    git(worktree, "reset", "-q", "--hard", sha)
    git(worktree, "clean", "-q", "-fd")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, codes=(0, 1)).returncode == 0


def would_conflict(repo: Path, ours: str, theirs: str) -> bool:
    return run_git(repo, "merge-tree", "--write-tree", ours, theirs, codes=(0, 1)).returncode == 1


def merge(worktree: Path, ref: str, message: str) -> list[str]:
    """Merge ref into HEAD. A clean merge is committed and returns []. A conflicted one is left in
    progress, markers in place, and returns the conflicted paths: the next accepted commit completes it."""
    result = run_git(worktree, "merge", "--no-ff", "--no-edit", "-m", message, ref, timeout=300, codes=(0, 1))
    if result.returncode == 0:
        return []
    conflicted = git(worktree, "diff", "--name-only", "--diff-filter=U").splitlines()
    if not conflicted:
        raise GitError(f"git merge {ref} failed: {(result.stdout + result.stderr).strip()[-2000:]}")
    return conflicted


def merging(worktree: Path) -> bool:
    return (worktree / git(worktree, "rev-parse", "--git-path", "MERGE_HEAD").strip()).exists()


MARKER = re.compile(r"^(<{7}|>{7}) ", re.MULTILINE)


def unresolved(worktree: Path) -> list[str]:
    """Files changed on both sides of an in-progress merge that still hold conflict markers. Derived
    from the commits, because `git add -N` (fingerprint, changed_files) drops the index's unmerged entries."""
    if not merging(worktree):
        return []
    base = git(worktree, "merge-base", "HEAD", "MERGE_HEAD").strip()
    ours = set(git(worktree, "diff", "--name-only", base, "HEAD").splitlines())
    theirs = set(git(worktree, "diff", "--name-only", base, "MERGE_HEAD").splitlines())
    return sorted(path for path in ours & theirs
                  if (worktree / path).is_file() and MARKER.search((worktree / path).read_text(errors="replace")))
