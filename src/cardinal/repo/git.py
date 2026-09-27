"""Git operations on the shared clone and per-issue worktrees. Every call raises on failure."""

import hashlib
import os
import subprocess
from pathlib import Path

import cardinal

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


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    result = subprocess.run(
        ["git", *IDENTITY, *args], cwd=repo, text=True, capture_output=True, check=False,
        timeout=timeout, env={**os.environ, **CREDENTIALS},
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result.stdout


def guard_target(url: str) -> None:
    """Cardinal works on clones of other repositories, never on its own checkout."""
    local = Path(url).expanduser()
    if local.exists() and local.resolve() == PRODUCT_ROOT:
        raise GitError(f"Refusing to operate on Cardinal's own checkout at {PRODUCT_ROOT}")


def ensure_clone(clone: Path, url: str, base: str) -> Path:
    guard_target(url)
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
    git(clone, "fetch", "-q", "origin", base, timeout=600)
    base_sha = git(clone, "rev-parse", f"refs/remotes/origin/{base}").strip()
    for stale in worktrees_on(clone, branch):  # a failed earlier run of this issue keeps its worktree
        remove_worktree(clone, stale)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(clone, "worktree", "add", "-q", "-B", branch, str(path), base_sha)
    return base_sha


def worktrees_on(clone: Path, branch: str) -> list[Path]:
    found, current = [], None
    for line in git(clone, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and current is not None:
            found.append(current)
    return found


def remove_worktree(clone: Path, path: Path) -> None:
    if path.exists():
        git(clone, "worktree", "remove", "--force", str(path))
    git(clone, "worktree", "prune")


def head(worktree: Path) -> str:
    return git(worktree, "rev-parse", "HEAD").strip()


def commit_all(worktree: Path, message: str) -> str:
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


def publish(worktree: Path, branch: str) -> str:
    """Push the branch. Overwrite a previous managed push only under an exact lease."""
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
