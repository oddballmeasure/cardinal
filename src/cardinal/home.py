"""Every path Cardinal owns lives under one home directory, never the process working directory."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def resolve(cls, explicit: Path | None = None) -> "Home":
        raw = explicit or os.environ.get("CARDINAL_HOME") or Path.home() / ".cardinal"
        return cls(Path(raw).expanduser().resolve())

    @property
    def config(self) -> Path:
        return self.root / "cardinal.toml"

    @property
    def store(self) -> Path:
        return self.root / "store.db"

    @property
    def checkpoints(self) -> Path:
        return self.root / "checkpoints.db"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def clone(self, slug: str) -> Path:
        return self.root / "clones" / safe(slug)

    def worktree(self, slug: str, issue: int, run_id: str) -> Path:
        return self.root / "worktrees" / safe(slug) / f"issue-{issue}-{run_id}"

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id


def safe(slug: str) -> str:
    if not SLUG.fullmatch(slug):
        raise ValueError(f"Repository must be owner/name, got {slug!r}")
    return slug.replace("/", "--")
