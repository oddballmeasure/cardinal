"""What an agent can see and touch.

Agents get no shell. The worktree is the only writable mount, and even there `.git`, vendor
directories and the repository's off-limits paths refuse writes. Hidden paths go further: agents
cannot read, list or search them, so a grader kept in the target repository stays unseen. Skills and run context are
mounted read-only, so an agent cannot rewrite its own instructions or the evidence it is judged
on. Vendor output is dropped before a listing or search result is returned to the model
(lessons/exclude-vendor-before-returning.md).
"""

from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (DeleteResult, EditResult, FileDownloadResponse, FileUploadResponse, GlobResult,
                                          GrepResult, LsResult, ReadResult, WriteResult)

VENDOR = {"node_modules", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
          ".ruff_cache", "dist", "build", ".next", "coverage", ".tox", ".cache"}
MAX_MATCHES = 200
MAX_LINE = 400


def vendored(path: str) -> bool:
    return any(part in VENDOR for part in path.strip("/").split("/"))


def under(path: str, prefixes: list[str]) -> bool:
    """True when path is one of the prefixes, or inside one of them."""
    relative = path.strip("/")
    cleaned = [prefix.strip("/") for prefix in prefixes if prefix.strip("/")]
    return any(relative == prefix or relative.startswith(prefix + "/") for prefix in cleaned)


class ReadOnlyBackend(FilesystemBackend):
    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"{file_path} is read-only")

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:  # noqa: FBT001, FBT002
        return EditResult(error=f"{file_path} is read-only")

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=f"{file_path} is read-only")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


class RepoBackend(FilesystemBackend):
    def __init__(self, root: Path, off_limits: list[str], hidden: list[str], *, writable: bool) -> None:
        super().__init__(root_dir=str(root), virtual_mode=True)
        self.off_limits = off_limits
        self.hidden = hidden
        self.writable = writable

    def _unlisted(self, path: str) -> bool:
        return vendored(path) or under(path, self.hidden)

    def _refusal(self, path: str) -> str | None:
        if not self.writable:
            return f"{path} is read-only in this stage"
        if vendored(path):
            return f"{path} is a vendor or git path and cannot be changed"
        if under(path, self.off_limits + self.hidden):
            return f"{path} is off limits for this repository"
        return None

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if under(file_path, self.hidden):
            return ReadResult(error=f"{file_path} is off limits for this repository")
        return super().read(file_path, offset, limit)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(path=path, error="permission_denied") if under(path, self.hidden)
                else super().download_files([path])[0] for path in paths]

    def ls(self, path: str) -> LsResult:
        result = super().ls(path)
        if result.entries is not None:
            result.entries = [entry for entry in result.entries if not self._unlisted(entry["path"])]
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        result = super().glob(pattern, path)
        if result.matches is not None:
            result.matches = [entry for entry in result.matches if not self._unlisted(entry["path"])]
        return result

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None,
             **options) -> GrepResult:
        result = super().grep(pattern, path, glob, max_count=max_count, **options)
        if result.matches is not None:
            kept = [match for match in result.matches if not self._unlisted(match["path"])]
            for match in kept:
                if len(match["text"]) > MAX_LINE:
                    match["text"] = match["text"][:MAX_LINE] + " …[line clipped]"
            if len(kept) > MAX_MATCHES:
                kept, result.truncated = kept[:MAX_MATCHES], True
            result.matches = kept
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        refusal = self._refusal(file_path)
        return WriteResult(error=refusal) if refusal else super().write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:  # noqa: FBT001, FBT002
        refusal = self._refusal(file_path)
        return EditResult(error=refusal) if refusal else super().edit(file_path, old_string, new_string, replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        refusal = self._refusal(file_path)
        return DeleteResult(error=refusal) if refusal else super().delete(file_path)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]


def mounts(worktree: Path | None, context: Path, skills: Path, off_limits: list[str], hidden: list[str], *,
           repo_writable: bool) -> CompositeBackend:
    """/repo/ is the worktree, /context/ the run's inputs, /skills/ the packaged instructions."""
    routes = {"/context/": ReadOnlyBackend(root_dir=str(context), virtual_mode=True),
              "/skills/": ReadOnlyBackend(root_dir=str(skills), virtual_mode=True)}
    if worktree is not None:
        routes["/repo/"] = RepoBackend(worktree, off_limits, hidden, writable=repo_writable)
    return CompositeBackend(default=ReadOnlyBackend(root_dir=str(context), virtual_mode=True), routes=routes)
