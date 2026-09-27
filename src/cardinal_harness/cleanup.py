"""The harness's cleaner agent: restores a test repository after a suite, from a pinned request.

This is harness tooling, not product: it force-resets a remote under an exact lease, so it keeps
its own skill (./skills/cleaner) and its own offline scenario.
"""

import json
import shutil
import subprocess
from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.chat_models import init_chat_model
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from cardinal_harness.cleaner import CleanerService, GitHubCleanerAPI, LocalCleanerGitHub
from cardinal_harness.contracts import CleanupRequest, Issue
from cardinal_harness.product import ROOT, json_file

STEPS = ["inspect_cleanup", "reset_remote", "close_managed_prs", "delete_managed_branches", "restore_issue", "verify_clean"]


class Scripted(GenericFakeChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):  # noqa: ANN001, ANN201
        return self


def scripted_cleaner() -> Scripted:
    calls = [AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/workspace/cleanup_request.json"}, "id": "request"}]),
             *(AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}]) for name in STEPS),
             AIMessage(content="Recorded the remote reset and issue state.")]
    return Scripted(messages=iter(calls))


def clean(work: Path, repo: Path, request: CleanupRequest, github, model_spec: str | None, artifact: Path) -> dict:
    """Run the cleaner agent in `work`; `repo` is a clone whose origin is the remote to restore."""
    (work / "workspace").mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "skills", work / "skills", dirs_exist_ok=True)
    json_file(work / "workspace" / "cleanup_request.json", request.model_dump())
    service = CleanerService(repo, request, github, artifact / "cleanup_events.json", artifact / "remote_state.json")
    model = scripted_cleaner() if model_spec is None else init_chat_model(model_spec, timeout=180, max_retries=1)
    agent = create_deep_agent(model=model, backend=FilesystemBackend(root_dir=str(work), virtual_mode=True),
                              skills=["/skills/"], tools=service.tools())
    agent.invoke({"messages": [{"role": "user", "content": (
        "Use the cleaner skill and /workspace/cleanup_request.json. Inspect the pinned remote, reset its base "
        "branch, close only managed open PRs, delete only managed branches, restore the issues, then verify.")}]},
        config={"recursion_limit": 80})
    result = service.result()
    json_file(artifact / "cleanup.json", result.model_dump())
    return result.model_dump()


def github_clean(work: Path, request: CleanupRequest, model_spec: str, artifact: Path) -> dict:
    repo = work / "workspace" / "repo"
    subprocess.run(["git", "clone", "-q", request.remote_url, str(repo)], check=True, capture_output=True, timeout=300)
    return clean(work, repo, request, GitHubCleanerAPI(request.repository), model_spec, artifact)


def local_scenario(temp: Path, artifact: Path, expected_head: str | None = None) -> dict:
    """A bare remote with a merged change, a closed issue and managed PRs, restored by the scripted cleaner."""
    def git(repo: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True).stdout.strip()

    repo, bare = temp / "repo", temp / "remote.git"
    artifact.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "tests" / "fixtures" / "repo", repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "-qm", "baseline")
    base = git(repo, "rev-parse", "HEAD")
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    git(repo, "remote", "add", "origin", str(bare))
    git(repo, "switch", "-q", "-c", "cardinal/104-fix")
    (repo / "CHANGE.md").write_text("merged change\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=F", "-c", "user.email=f@example.invalid", "commit", "-qm", "change")
    merged = git(repo, "rev-parse", "HEAD")
    git(repo, "push", "-q", "origin", f"{base}:refs/heads/main", "cardinal/104-fix")
    git(repo, "push", "-q", "origin", f"{merged}:refs/heads/main")
    issue = Issue.model_validate_json((ROOT / "tests" / "fixtures" / "issue_104.json").read_text())
    request = CleanupRequest(repository=issue.repository, remote_url=str(bare), base_branch="main", base_sha=base,
                             expected_head_sha=expected_head or merged, managed_branches=["cardinal/104-fix"],
                             managed_pr_numbers=[7, 8], issue_action="reopen", issue_number=issue.number)
    github = LocalCleanerGitHub(artifact / "cleaner_github.json", issue.model_dump())
    try:
        result = clean(temp / "work", repo, request, github, None, artifact)
    except ValueError as exc:
        result = {"clean": False, "refused": str(exc)}
    remote = {"main": git(repo, "ls-remote", "origin", "refs/heads/main").split()[0],
              "branches": git(repo, "ls-remote", "--heads", "origin")}
    state = json.loads((artifact / "cleaner_github.json").read_text())
    return {"base": base, "merged": merged, "result": result, "remote": remote, "github": state}
