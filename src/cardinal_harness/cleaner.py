"""Reset an explicitly scoped test repository and issue state."""

import json
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from cardinal_harness.contracts import CleanupRequest, CleanupResult


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=True, timeout=30).stdout


def remote_sha(repo: Path, branch: str) -> str | None:
    lines = git(repo, "ls-remote", "origin", f"refs/heads/{branch}").splitlines()
    return lines[0].split()[0] if lines else None


class LocalCleanerGitHub:
    def __init__(self, artifact: Path, issue: dict) -> None:
        self.artifact = artifact
        self.state = {
            "issues": {str(issue["number"]): {"state": "closed", "title": issue["title"], "body": issue["body"]}},
            "pull_requests": {"7": {"state": "merged"}, "8": {"state": "open"}},
            "next_issue_number": issue["number"] + 1,
        }
        self.save()

    def save(self) -> None:
        self.artifact.write_text(json.dumps(self.state, indent=2) + "\n")

    def pr(self, number: int) -> dict:
        return self.state["pull_requests"][str(number)]

    def close_pr(self, number: int) -> None:
        self.pr(number)["state"] = "closed"
        self.save()

    def issue(self, number: int) -> dict:
        return self.state["issues"][str(number)]

    def reopen_issue(self, number: int) -> None:
        self.issue(number)["state"] = "open"
        self.save()

    def create_issue(self, title: str, body: str) -> int:
        number = self.state["next_issue_number"]
        self.state["next_issue_number"] += 1
        self.state["issues"][str(number)] = {"state": "open", "title": title, "body": body}
        self.save()
        return number

    def replace_labels(self, number: int, labels: list[str]) -> None:
        self.issue(number)["labels"] = [{"name": label} for label in labels]
        self.save()


class GitHubCleanerAPI:
    def __init__(self, repository: str) -> None:
        self.repository = repository

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        command = ["gh", "api", "-X", method, f"repos/{self.repository}/{path}"]
        if body is not None:
            command.extend(["--input", "-"])
        result = subprocess.run(
            command, input=json.dumps(body) if body is not None else None,
            text=True, capture_output=True, check=True, timeout=30,
        )
        return json.loads(result.stdout)

    def pr(self, number: int) -> dict:
        value = self.api("GET", f"pulls/{number}")
        return {"state": "merged" if value.get("merged_at") else value["state"]}

    def close_pr(self, number: int) -> None:
        self.api("PATCH", f"pulls/{number}", {"state": "closed"})

    def issue(self, number: int) -> dict:
        return self.api("GET", f"issues/{number}")

    def reopen_issue(self, number: int) -> None:
        self.api("PATCH", f"issues/{number}", {"state": "open", "state_reason": "reopened"})

    def create_issue(self, title: str, body: str) -> int:
        return self.api("POST", "issues", {"title": title, "body": body})["number"]

    def replace_labels(self, number: int, labels: list[str]) -> None:
        self.api("PATCH", f"issues/{number}", {"labels": labels})


class CleanerService:
    ACTIONS = ["inspect", "reset_remote", "close_prs", "delete_branches", "restore_issue", "verify_clean"]

    def __init__(self, repo: Path, request: CleanupRequest, github, artifact: Path, remote_artifact: Path) -> None:
        self.repo = repo
        self.request = request
        self.github = github
        self.artifact = artifact
        self.remote_artifact = remote_artifact
        self.events: list[dict] = []
        self.step = 0
        self.closed_pr_numbers: list[int] = []
        self.historical_merged_pr_numbers: list[int] = []
        self.deleted_branches: list[str] = []
        self.issue_number: int | None = None
        self.issue_numbers: list[int] = []
        for branch in [request.base_branch, *request.managed_branches]:
            valid = subprocess.run(
                ["git", "check-ref-format", "--branch", branch], cwd=repo,
                capture_output=True, check=False, timeout=15,
            )
            if valid.returncode != 0:
                raise ValueError(f"Invalid cleanup branch: {branch}")
        self.previous_sha = remote_sha(repo, request.base_branch)
        if git(repo, "remote", "get-url", "origin").strip() != request.remote_url:
            raise ValueError("Configured remote URL does not match origin")
        git(repo, "cat-file", "-e", f"{request.base_sha}^{{commit}}")
        if request.base_branch in request.managed_branches:
            raise ValueError("Base branch cannot be a managed branch")
        self.save_remote()
        if self.previous_sha not in {request.expected_head_sha, request.base_sha}:
            raise ValueError("Remote head does not match the expected merged head")

    def save_remote(self) -> None:
        self.remote_artifact.write_text(json.dumps({
            "main_sha": remote_sha(self.repo, self.request.base_branch),
            "managed_branch_sha": remote_sha(self.repo, self.request.managed_branches[0]) if self.request.managed_branches else None,
            "managed_branches": {branch: remote_sha(self.repo, branch) for branch in self.request.managed_branches},
        }, indent=2) + "\n")

    def record(self, action: str, detail: dict) -> str:
        self.events.append({"action": action, **detail})
        self.artifact.write_text(json.dumps(self.events, indent=2) + "\n")
        self.step += 1
        self.save_remote()
        return json.dumps(detail)

    def require(self, action: str) -> None:
        if self.step >= len(self.ACTIONS) or self.ACTIONS[self.step] != action:
            raise ValueError(f"Cleaner step out of order: {action}")

    def tools(self) -> list:
        @tool("inspect_cleanup")
        def inspect_cleanup() -> str:
            """Read the pinned reset request and current remote head before changes."""
            self.require("inspect")
            return self.record("inspect", {"remote_sha": self.previous_sha, "base_sha": self.request.base_sha})

        @tool("reset_remote")
        def reset_remote() -> str:
            """Move the configured remote branch to the pinned base with an exact lease."""
            self.require("reset_remote")
            current = remote_sha(self.repo, self.request.base_branch)
            if current != self.request.base_sha:
                if current != self.request.expected_head_sha:
                    raise ValueError("Remote head changed before reset")
                ref = f"refs/heads/{self.request.base_branch}"
                git(self.repo, "push", f"--force-with-lease={ref}:{current}", "origin", f"{self.request.base_sha}:{ref}")
            return self.record("reset_remote", {"remote_sha": remote_sha(self.repo, self.request.base_branch)})

        @tool("close_managed_prs")
        def close_managed_prs() -> str:
            """Close open managed PRs and retain historical merged PR records."""
            self.require("close_prs")
            for number in self.request.managed_pr_numbers:
                state = self.github.pr(number)["state"]
                if state == "open":
                    self.github.close_pr(number)
                    self.closed_pr_numbers.append(number)
                elif state == "merged":
                    self.historical_merged_pr_numbers.append(number)
            return self.record("close_prs", {"closed": self.closed_pr_numbers, "merged_history": self.historical_merged_pr_numbers})

        @tool("delete_managed_branches")
        def delete_managed_branches() -> str:
            """Delete only listed remote feature branches using exact ref leases."""
            self.require("delete_branches")
            for branch in self.request.managed_branches:
                current = remote_sha(self.repo, branch)
                if current:
                    ref = f"refs/heads/{branch}"
                    git(self.repo, "push", f"--force-with-lease={ref}:{current}", "origin", f":{ref}")
                    self.deleted_branches.append(branch)
            return self.record("delete_branches", {"deleted": self.deleted_branches})

        @tool("restore_issue")
        def restore_issue() -> str:
            """Reopen or create the managed issues and restore their labels."""
            self.require("restore_issue")
            resets = self.request.issue_resets or [{"action": self.request.issue_action,
                "number": self.request.issue_number, "title": self.request.issue_title,
                "body": self.request.issue_body, "labels": []}]
            for reset in resets:
                item = reset if isinstance(reset, dict) else reset.model_dump()
                if item["action"] == "reopen":
                    number = item["number"]
                    if number is None:
                        raise ValueError("Issue number required to reopen")
                    if self.github.issue(number)["state"] != "open":
                        self.github.reopen_issue(number)
                else:
                    if not item["title"] or item["body"] is None:
                        raise ValueError("Issue title and body required to create")
                    number = self.github.create_issue(item["title"], item["body"])
                if item["labels"]:
                    self.github.replace_labels(number, item["labels"])
                self.issue_numbers.append(number)
            self.issue_number = self.issue_numbers[0]
            return self.record("restore_issue", {"issue_numbers": self.issue_numbers})

        @tool("verify_clean")
        def verify_clean() -> str:
            """Reread the remote branch, managed PRs and branches, and restored issue."""
            self.require("verify_clean")
            clean = (
                remote_sha(self.repo, self.request.base_branch) == self.request.base_sha
                and all(remote_sha(self.repo, branch) is None for branch in self.request.managed_branches)
                and all(self.github.pr(number)["state"] != "open" for number in self.request.managed_pr_numbers)
                and bool(self.issue_numbers)
                and all(self.github.issue(number)["state"] == "open" for number in self.issue_numbers)
                and all(
                    sorted(label["name"] for label in self.github.issue(number).get("labels", [])) == sorted(reset.labels)
                    for reset, number in zip(self.request.issue_resets, self.issue_numbers)
                )
            )
            return self.record("verify_clean", {"clean": clean})

        return [inspect_cleanup, reset_remote, close_managed_prs, delete_managed_branches, restore_issue, verify_clean]

    def result(self) -> CleanupResult:
        if self.step != len(self.ACTIONS) or not self.events[-1]["clean"]:
            raise ValueError("Cleaner did not verify the complete remote state")
        return CleanupResult(
            repository=self.request.repository,
            base_branch=self.request.base_branch,
            base_sha=self.request.base_sha,
            previous_sha=self.previous_sha,
            remote_sha=remote_sha(self.repo, self.request.base_branch),
            issue_number=self.issue_number,
            issue_numbers=self.issue_numbers,
            closed_pr_numbers=self.closed_pr_numbers,
            historical_merged_pr_numbers=self.historical_merged_pr_numbers,
            deleted_branches=self.deleted_branches,
            clean=True,
        )
