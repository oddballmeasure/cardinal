"""Live dependencies for one run. Held outside graph state because none of it serialises."""

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cardinal.config.models import Config, Repo
from cardinal.contracts.issue import Issue
from cardinal.home import Home
from cardinal.store.recorder import Recorder


@dataclass
class Run:
    home: Home
    config: Config
    repo: Repo
    db: sqlite3.Connection
    recorder: Recorder
    run_id: str
    issue: Issue

    @property
    def clone(self) -> Path:
        return self.home.clone(self.repo.slug)

    @property
    def worktree(self) -> Path:
        return self.home.worktree(self.repo.slug, self.issue.number, self.run_id)

    @property
    def context_dir(self) -> Path:
        path = self.home.run_dir(self.run_id) / "context"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def branch(self) -> str:
        return f"{self.repo.branch_prefix}/{self.issue.number}-{slug(self.issue.title)}"

    def write_context(self, name: str, value: object) -> None:
        target = self.context_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, str):
            target.write_text(value)
        else:
            target.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")

    def agent_context(self, **extra: object) -> dict:
        """What a model-provider hook may see about the call it is serving."""
        return {"repo": self.repo.slug, "issue": self.issue.model_dump(), "run_id": self.run_id,
                "worktree": str(self.worktree), "branch": self.branch, **extra}


def slug(title: str) -> str:
    words = "".join(ch.lower() if ch.isalnum() else " " for ch in title).split()
    return "-".join(words)[:40].strip("-") or "issue"


@dataclass
class ScoutRun:
    """A scout pass: like a Run, but over a detached worktree at the base branch, with no issue. The
    profiler takes either; context_dir and write_context are Run's own, so both lay out /context/ alike."""

    home: Home
    config: Config
    repo: Repo
    db: sqlite3.Connection
    recorder: Recorder
    run_id: str
    worktree: Path

    clone = Run.clone
    context_dir = Run.context_dir
    write_context = Run.write_context

    def agent_context(self, **extra: object) -> dict:
        return {"repo": self.repo.slug, "run_id": self.run_id, "worktree": str(self.worktree), **extra}
