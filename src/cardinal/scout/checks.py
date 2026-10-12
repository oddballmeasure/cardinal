"""Checks code makes on a proposal before anyone spends attention on it: every quote is in the file
at the base commit, every path is one agents may touch, and every acceptance criterion names a value."""

import hashlib
import re
from pathlib import Path

from cardinal.agents.backends import under
from cardinal.config.models import Repo, Scout
from cardinal.contracts.scout import Evidence, Proposal

# A criterion is observable when it names an exact value: a number (status, exit code, count), a
# quoted literal (message, label, field name) or a JSON literal ([], {}, null, true, false).
OBSERVABLE = re.compile(r'\d|`[^`]+`|"[^"]+"|\[\]|\{\}|\b(?:null|true|false)\b')
MAX_SPAN = 40
DRIFT = 10  # models miscount lines; a quote found this close to its cited range is the same evidence
SIMILAR = 0.6


def normalise(text: str) -> str:
    return " ".join(text.split())


def locate(lines: list[str], item: Evidence) -> tuple[int, int] | None:
    """Find actual quote occurrences, preferring one close to the citation or unique in the file."""
    quote = normalise(item.quote)
    if not quote:
        return None
    # Join non-whitespace runs exactly as normalise does, retaining the source line for each
    # character so a match shorter than the cited span has its own precise range.
    text, line_numbers = [], []
    for line_no, line in enumerate(lines, 1):
        for token in re.finditer(r"\S+", line):
            if text:
                text.append(" ")
                line_numbers.append(line_no)
            text.append(token.group())
            line_numbers.extend([line_no] * len(token.group()))
    source = "".join(text)
    found = []
    offset = 0
    while (offset := source.find(quote, offset)) != -1:
        found.append((line_numbers[offset], line_numbers[offset + len(quote) - 1]))
        offset += 1  # count occurrences, not overlapping windows containing the same occurrence
    def distance(match: tuple[int, int]) -> int:
        start, end = match
        return max(item.line_start - end, start - item.line_end, 0)

    nearby = [match for match in found if distance(match) <= DRIFT]
    if nearby:
        return min(nearby, key=distance)
    return found[0] if len(found) == 1 else None


def evidence_problem(worktree: Path, items: list[Evidence], tracked: set[str], repo: Repo) -> str | None:
    """Checks every quote and, where a model miscounted lines, corrects the cited range in place."""
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
        found = locate(lines, item)
        if found is None:
            if item.line_start > len(lines):
                return f"evidence {where} is past the end of the file ({len(lines)} lines)"
            return f"evidence {where} does not contain the quoted text, nor do the {DRIFT} lines around it"
        if found != (item.line_start, item.line_end):
            if item.corrected_from is None:
                item.corrected_from = (item.line_start, item.line_end)
            item.line_start, item.line_end = found
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
