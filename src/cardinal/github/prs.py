"""The PR manager's tools. Repository, branch, base and head are pinned; the model chooses only
whether to reuse or create, and when to merge. Tool misuse returns an error the model can read and
is recorded as a violation the runtime rejects afterwards."""

import json
import re
import time
from pathlib import Path

from langchain_core.tools import tool

from cardinal.contracts.pr import PRResult
from cardinal.contracts.verdict import Verdict
from cardinal.github.gh import GhError, gh, gh_json
from cardinal.repo.git import remote_sha


def check_state(repo: str, sha: str, required: list[str]) -> str:
    """success | failure | pending for the required checks on one exact commit."""
    if not required:
        return "success"
    runs = gh_json("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
    states = []
    for name in required:
        matching = [run for run in runs if run["name"] == name and run["head_sha"] == sha]
        if not matching:
            states.append("pending")
            continue
        latest = max(matching, key=lambda run: run["id"])
        if latest["status"] != "completed":
            states.append("pending")
        else:
            states.append("success" if latest["conclusion"] in {"success", "skipped", "neutral"} else "failure")
    if "failure" in states:
        return "failure"
    return "pending" if "pending" in states else "success"


LOG_TAIL = 8000
STAMP = re.compile(r"^\S+Z ", re.MULTILINE)
ESCAPES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def failure_log(repo: str, sha: str, required: list[str]) -> str:
    """The tail of each failed required check's log on one commit, as plain text a coder can read.
    For GitHub Actions a check run's id is its job id; other checks only offer their summary."""
    runs = gh_json("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100")["check_runs"]
    parts = []
    for name in required:
        matching = [run for run in runs if run["name"] == name and run["head_sha"] == sha]
        if not matching:
            continue
        latest = max(matching, key=lambda run: run["id"])
        if latest["conclusion"] in {"success", "skipped", "neutral"}:
            continue
        try:
            text = gh("api", f"repos/{repo}/actions/jobs/{latest['id']}/logs", "--allow-escape-sequences", timeout=120)
        except GhError:
            output = latest.get("output") or {}
            text = "\n".join(filter(None, [output.get("summary"), output.get("text")])) or "(no log available)"
        text = ESCAPES.sub("", STAMP.sub("", text))
        parts.append(f"Check `{name}` concluded {latest['conclusion']} on {sha[:12]}. Log tail:\n{text[-LOG_TAIL:]}")
    return "\n\n".join(parts) or f"Required checks {required} failed on {sha[:12]}; no log was available."


class PRService:
    def __init__(self, clone: Path, repo: str, base: str, branch: str, verdict: Verdict,
                 required_checks: list[str], ci_timeout: int, poll_seconds: int, closes: int) -> None:
        if not verdict.approved or verdict.branch != branch:
            raise ValueError("PR manager requires an approved verdict for this branch")
        self.clone, self.repo, self.base, self.branch = clone, repo, base, branch
        self.head_sha = verdict.head_sha
        self.required = required_checks
        self.ci_timeout, self.poll_seconds = ci_timeout, poll_seconds
        self.closes = closes
        self.number: int | None = None
        self.url: str | None = None
        self.created = False
        self.merge_sha: str | None = None
        self.ci_observations: list[str] = []
        self.events: list[dict] = []
        self.violations: list[str] = []

    def _violation(self, message: str) -> str:
        self.violations.append(message)
        return f"error: {message}"

    def _open_pr(self) -> dict | None:
        prs = gh_json("pr", "list", "-R", self.repo, "--state", "open", "--head", self.branch,
                      "--json", "number,url,headRefOid,headRefName,baseRefName")
        return prs[0] if prs else None

    def tools(self) -> list:
        @tool("list_open_prs")
        def list_open_prs() -> str:
            """Find an open PR for the pinned branch."""
            pr = self._open_pr()
            if pr:
                self.number, self.url = pr["number"], pr["url"]
            self.events.append({"action": "list_open_prs", "pr": pr})
            return json.dumps([pr] if pr else [])

        @tool("create_pull_request")
        def create_pull_request(title: str, body: str) -> str:
            """Open one PR from the pinned branch into the base branch. Only when none is open."""
            if self.number is not None:
                return self._violation("a PR is already open for this branch; reuse it")
            if self._open_pr() is not None:
                return self._violation("a PR is already open for this branch; call list_open_prs")
            if f"closes #{self.closes}" not in body.lower():
                body = f"Closes #{self.closes}\n\n{body}"
            gh("pr", "create", "-R", self.repo, "--head", self.branch, "--base", self.base,
               "--title", title, "--body", body)
            pr = self._open_pr()
            if pr is None or pr["headRefOid"] != self.head_sha:
                raise GhError("Created PR is missing or does not point at the verified head")
            self.number, self.url, self.created = pr["number"], pr["url"], True
            self.events.append({"action": "create_pull_request", "number": self.number})
            return json.dumps({"number": self.number, "url": self.url, "head_sha": self.head_sha})

        @tool("wait_for_ci")
        def wait_for_ci(number: int) -> str:
            """Wait until the required checks finish on the verified head. Returns success, failure or timeout."""
            if number != self.number:
                return self._violation(f"PR {number} is not this branch's PR")
            deadline = time.monotonic() + self.ci_timeout
            while True:
                pr = gh_json("pr", "view", str(number), "-R", self.repo, "--json", "headRefOid,state")
                if pr["headRefOid"] != self.head_sha or pr["state"] != "OPEN":
                    return self._violation("the PR head or state changed while waiting for CI")
                status = check_state(self.repo, self.head_sha, self.required)
                self.ci_observations.append(status)
                self.events.append({"action": "ci", "status": status})
                if status != "pending":
                    return json.dumps({"status": status, "head_sha": self.head_sha, "checks": self.required})
                if time.monotonic() >= deadline:
                    self.ci_observations.append("timeout")
                    return json.dumps({"status": "timeout", "head_sha": self.head_sha})
                time.sleep(self.poll_seconds)

        @tool("merge_pull_request")
        def merge_pull_request(number: int) -> str:
            """Merge the PR, only after wait_for_ci reported success for the verified head."""
            if number != self.number:
                return self._violation(f"PR {number} is not this branch's PR")
            if not self.ci_observations or self.ci_observations[-1] != "success":
                return self._violation("merge requested without successful CI on the verified head")
            gh("pr", "merge", str(number), "-R", self.repo, "--merge",
               "--match-head-commit", self.head_sha, timeout=180)
            self.merge_sha = remote_sha(self.clone, self.base)
            self.events.append({"action": "merge", "number": number, "merge_sha": self.merge_sha})
            return json.dumps({"merged": True, "merge_sha": self.merge_sha})

        return [list_open_prs, create_pull_request, wait_for_ci, merge_pull_request]

    def result(self) -> PRResult:
        status = "merged" if self.merge_sha else "blocked_ci" if self.number else "not_opened"
        return PRResult(number=self.number, url=self.url, branch=self.branch, head_sha=self.head_sha,
                        created=self.created, status=status, ci_observations=self.ci_observations,
                        merge_sha=self.merge_sha)
