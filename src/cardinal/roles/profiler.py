"""Profile tracked files in bounded batches, re-profiling only files whose content changed."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from cardinal.agents.backends import under
from cardinal.agents.runner import AgentCall, run_agent
from cardinal.contracts.profile import ProfileChunk, RepoProfile
from cardinal.graph.context import Run
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.repo.git import head
from cardinal.store import inputs

BATCH_SIZE = 5
MAX_LINES = 150


def tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True, timeout=30)
    return sorted(path.decode() for path in result.stdout.split(b"\0") if path)


def file_hash(repo: Path, path: str) -> str:
    source = repo / path
    digest = hashlib.sha256()
    if source.is_symlink():  # profile the link, never follow it out of the repository
        digest.update(b"link:" + os.readlink(source).encode())
    elif source.is_dir():  # submodule
        digest.update(b"dir:" + path.encode())
    else:
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    return digest.hexdigest()


def content_hash(hashes: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(hashes):
        digest.update(f"{path}\0{hashes[path]}\n".encode())
    return digest.hexdigest()


def read_bounded(repo: Path, path: str, start_line: int, max_lines: int) -> str:
    source = repo / path
    if source.is_symlink():
        return f"Symlink to {os.readlink(source)}"
    if source.is_dir():
        return f"Git submodule: {path}"
    lines: list[str] = []
    with source.open("r", errors="replace") as handle:
        for number, line in enumerate(iter(lambda: handle.readline(2048), ""), start=1):
            if number >= start_line:
                lines.append(f"{number}: {line.rstrip()}")
            if number >= start_line + max_lines - 1 or sum(map(len, lines)) >= 16000:
                break
    return "\n".join(lines) or "<empty or past end of file>"


def profile(run: Run) -> RepoProfile:
    repo = run.worktree
    paths = [path for path in tracked_files(repo) if not under(path, run.repo.paths_hidden)]
    if not paths:
        raise StageFailure(FailureKind.GIT, "Repository has no tracked files to profile")
    hashes = {path: file_hash(repo, path) for path in paths}
    digest = content_hash(hashes)
    previous = inputs.load_profile(run.db, run.repo.slug)
    if previous is not None and previous.content_hash == digest:
        run.recorder.event("profile", "reused", {"revision": previous.revision, "files": len(paths)})
        return previous
    kept = {entry.path: entry for entry in (previous.files if previous else [])
            if previous and previous.file_hashes.get(entry.path) == hashes.get(entry.path)}
    stale = [path for path in paths if path not in kept]
    batches = [stale[index:index + BATCH_SIZE] for index in range(0, len(stale), BATCH_SIZE)]
    for number, batch in enumerate(batches, start=1):
        kept.update({entry.path: entry for entry in profile_batch(run, repo, batch, number, len(batches))})
    if content_hash({path: file_hash(repo, path) for path in paths}) != digest:
        raise StageFailure(FailureKind.GIT, "Repository changed during profiling")
    result = RepoProfile(repository=run.repo.slug, revision=head(repo), content_hash=digest,
                         file_hashes=hashes, files=[kept[path] for path in paths])
    inputs.save_profile(run.db, result)
    run.recorder.event("profile", "profiled", {"revision": result.revision, "files": len(paths),
                                               "reprofiled": len(stale), "batches": len(batches)})
    return result


def profile_batch(run: Run, repo: Path, batch: list[str], number: int, total: int):
    run.write_context("profile_batch.json", [{"path": path, "bytes": (repo / path).lstat().st_size} for path in batch])
    read_paths: set[str] = set()

    @tool("read_repo_file")
    def read_repo_file(path: str, start_line: int = 1, max_lines: int = MAX_LINES) -> str:
        """Read a bounded line range of one file listed in /context/profile_batch.json."""
        if path not in batch:
            return "Invalid path: choose a file listed in /context/profile_batch.json"
        if start_line < 1 or not 1 <= max_lines <= MAX_LINES:
            return f"Invalid range: start_line must be >= 1 and max_lines between 1 and {MAX_LINES}"
        read_paths.add(path)
        return read_bounded(repo, path, start_line, max_lines)

    call = AgentCall(
        stage="profiler", model_spec=run.config.models.profiler, context_dir=run.context_dir,
        context=run.agent_context(batch=batch, repo_path=str(repo)),
        prompt=(f"Use the profiler skill. Profile every file in /context/profile_batch.json (batch {number}/{total}). "
                "Call read_repo_file for each listed path, then return a ProfileChunk with exactly one entry per path."),
        tools=[read_repo_file], schema=ProfileChunk,
    )
    for attempt in (1, 2):
        chunk = run_agent(call, run.recorder, run.config.limits.recursion_limit, run.config.limits.model_timeout_seconds)
        described = sorted(entry.path for entry in chunk.files)
        if described == sorted(batch) and read_paths == set(batch):
            return chunk.files
        missing = sorted(set(batch) - set(described) | set(batch) - read_paths)
        call.prompt += (f"\n\nYour previous answer was rejected: every listed path must be read and described exactly "
                        f"once. Problem paths: {missing}.")
    raise StageFailure(FailureKind.INVALID_OUTPUT, f"Profiler did not read and describe batch {number}: {batch}")


def relevant_files(profile: RepoProfile, query: str, limit: int = 5) -> list[dict]:
    words = set(re.findall(r"[a-z0-9]+", query.lower())) - {"the", "and", "for", "with", "from", "active"}
    include_legacy = bool(words & {"legacy", "old", "prototype", "unused"})
    scored = []
    for entry in profile.files:
        if entry.status == "legacy" and not include_legacy:
            continue
        text = f"{entry.path} {entry.summary} {' '.join(entry.capabilities)}".lower()
        score = sum(3 if word in entry.path.lower() else 1 for word in words if word in text)
        if score:
            scored.append((score, entry.path, entry))
    return [entry.model_dump() for _, _, entry in sorted(scored, key=lambda item: (-item[0], item[1]))[:limit]]


def dump(profile: RepoProfile) -> str:
    return json.dumps({"repository": profile.repository, "revision": profile.revision,
                       "files": [entry.model_dump() for entry in profile.files]}, indent=2, ensure_ascii=False)
