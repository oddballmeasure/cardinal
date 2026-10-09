"""Checks code makes on a proposal before anyone spends attention on it: every quote is in the file
at the base commit, every path is one agents may touch, and every acceptance criterion names a value."""

import hashlib
import re
from pathlib import Path

from cardinal.agents.backends import under
from cardinal.config.models import Repo, Scout
from cardinal.contracts.scout import Evidence, Proposal

# A criterion is observable when it names an exact value: a number (status, exit code, count) or a
# quoted literal (message, label, field name).
OBSERVABLE = re.compile(r'\d|`[^`]+`|"[^"]+"')
MAX_SPAN = 40
SIMILAR = 0.6


def normalise(text: str) -> str:
    return " ".join(text.split())


def evidence_problem(worktree: Path, items: list[Evidence], tracked: set[str], repo: Repo) -> str | None:
    for item in items:
        where = f"{item.path}:{item.line_start}-{item.line_end}"
        source = worktree / item.path
        if item.path not in tracked or source.is_symlink() or not source.is_file():
            return f"evidence {item.path} is not a tracked file"
        if under(item.path, repo.paths_off_limits + repo.paths_hidden):
            return f"evidence {item.path} is off limits"
        if not 0 <= item.line_end - item.line_start < MAX_SPAN:
            return f"evidence {where} is not a range of 1-{MAX_SPAN} lines"
        lines = source.read_text(errors="replace").splitlines()
        if item.line_end > len(lines):
            return f"evidence {where} is past the end of the file ({len(lines)} lines)"
        if normalise(item.quote) not in normalise("\n".join(lines[item.line_start - 1:item.line_end])):
            return f"evidence {where} does not contain the quoted text"
    return None


def proposal_problem(worktree: Path, proposal: Proposal, tracked: set[str], repo: Repo, settings: Scout) -> str | None:
    if proposal.category not in settings.categories:
        return f"category {proposal.category} is not enabled"
    if len(set(proposal.files)) > settings.max_files:
        return f"a fix would change {len(set(proposal.files))} files; the limit is {settings.max_files}"
    for path in proposal.files:
        if path not in tracked:
            return f"file to change {path} is not a tracked file"
        if under(path, repo.paths_off_limits + repo.paths_hidden):
            return f"file to change {path} is off limits"
    vague = [item for item in proposal.acceptance if not OBSERVABLE.search(item)]
    if vague:
        return f"acceptance criterion names no exact value: {vague[0]!r}"
    return evidence_problem(worktree, proposal.evidence, tracked, repo)


def words(title: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", title.lower()) if len(word) > 2}


def similar(left: str, right: str) -> bool:
    a, b = words(left), words(right)
    return bool(a and b) and len(a & b) / len(a | b) >= SIMILAR


def fingerprint(proposal: Proposal) -> str:
    paths = sorted({item.path for item in proposal.evidence})
    key = "\0".join([proposal.category, *paths, " ".join(sorted(words(proposal.title)))])
    return hashlib.sha256(key.encode()).hexdigest()[:16]
