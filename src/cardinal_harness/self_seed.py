"""Seed the self-test repository: a one-commit snapshot of this checkout's HEAD, pushed to its own
origin, with the self suite's issues filed and labelled ready.

The snapshot leaves out tests/acceptance/self, the oracle that grades the issues
(lessons/product-never-sees-the-oracle.md), and adds a CI workflow that runs the snapshot's own
offline suite. A seeded origin is replaced only with --reseed, under an exact lease.
"""

import json
import shutil
import subprocess
from pathlib import Path

from cardinal_harness.contracts import Issue
from cardinal_harness.github_setup import gh, use_github_cli_git_credentials, wait_baseline_ci
from cardinal_harness.live import Target, remote_main
from cardinal_harness.product import ROOT, json_file, product_env

SUITE = ROOT / "tests" / "fixtures" / "self_suite.json"
ORACLE = "tests/acceptance/self"
IDENTITY = ["-c", "user.name=Cardinal Harness", "-c", "user.email=harness@users.noreply.github.com"]
LABELS = {"cardinal:ready": "0e8a16", "cardinal:difficulty/easy": "C2E0C6",
          "cardinal:difficulty/medium": "F9D0C4", "cardinal:difficulty/hard": "D93F0B"}
WORKFLOW = """\
name: offline-e2e

on:
  push:
    branches:
      - main
      - cardinal/**
  pull_request:
    branches:
      - main

permissions:
  contents: read

jobs:
  offline-e2e:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v7
        with:
          python-version: '3.13'
      - run: python -m pip install uv
      - run: uv run --locked python -m pytest -q
"""


def git(repo: Path, *args: str, timeout: int = 300) -> str:
    return subprocess.run(["git", *IDENTITY, *args], cwd=repo, text=True, capture_output=True, check=True,
                          timeout=timeout).stdout.strip()


def checkout(url: str, path: Path) -> Path:
    if not (path / ".git").is_dir():
        subprocess.run(["git", "clone", "-q", url, str(path)], text=True, capture_output=True, check=True, timeout=300)
    if git(path, "remote", "get-url", "origin").rstrip("/").removesuffix(".git") != url:
        raise ValueError(f"{path} does not point at {url}")
    if git(path, "status", "--porcelain"):
        raise ValueError(f"{path} has local changes; refusing to overwrite them")
    return path


def snapshot(repo: Path) -> str:
    """Replace the checkout's files with HEAD of this checkout, minus the oracle, as one commit."""
    if git(ROOT, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Commit Cardinal's tracked changes first; the snapshot is taken from HEAD")
    source = git(ROOT, "rev-parse", "HEAD")
    for entry in repo.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    archive = subprocess.run(["git", "archive", "HEAD", "--", ".", f":(exclude){ORACLE}"], cwd=ROOT,
                             capture_output=True, check=True, timeout=120).stdout
    subprocess.run(["tar", "-x", "-C", str(repo)], input=archive, check=True, timeout=120)
    workflow = repo / ".github" / "workflows" / "offline-e2e.yml"
    workflow.parent.mkdir(parents=True, exist_ok=True)
    workflow.write_text(WORKFLOW)
    git(repo, "checkout", "-q", "--orphan", "seed")
    git(repo, "add", "-A")
    if any(path.startswith(ORACLE) for path in git(repo, "ls-files").splitlines()):
        raise ValueError(f"{ORACLE} reached the snapshot")
    git(repo, "commit", "-q", "-m", f"Cardinal baseline {source[:12]}")
    git(repo, "branch", "-M", "main")
    return source


def ensure_issues(repository: str, suite: dict) -> list[dict]:
    for name, color in LABELS.items():
        gh("label", "create", name, "-R", repository, "--color", color, "--force")
    open_issues = json.loads(gh("issue", "list", "-R", repository, "--state", "open", "--limit", "100",
                                "--json", "number,title"))
    filed = []
    for case in suite["cases"]:
        template = Issue.model_validate_json((ROOT / case["issue_fixture"]).read_text())
        labels = [suite["ready_label"], f"cardinal:difficulty/{case['difficulty']}"]
        existing = [item["number"] for item in open_issues if item["title"] == template.title]
        if existing:
            gh("issue", "edit", str(existing[0]), "-R", repository, "--add-label", ",".join(labels))
            number = existing[0]
        else:
            url = gh("issue", "create", "-R", repository, "--title", template.title, "--body", template.body,
                     "--label", ",".join(labels)).strip().splitlines()[-1]
            number = int(url.rsplit("/", 1)[1])
        filed.append({"key": case["key"], "issue": number})
    return filed


def seed(reseed: bool, artifact: Path) -> dict:
    suite = json.loads(SUITE.read_text())
    target = Target.of(suite)
    repository, url = suite["repository"], f"https://github.com/{suite['repository']}"
    use_github_cli_git_credentials()
    repo = checkout(url, target.checkout)
    try:
        previous = remote_main(url)
    except IndexError:  # an empty repository has no main yet
        previous = None
    if previous and not reseed:
        raise ValueError(f"{repository} is already seeded at {previous}; pass --reseed to replace it")
    source = snapshot(repo)
    preflight = subprocess.run(target.test_command, cwd=repo, env=product_env({}), text=True, capture_output=True,
                               check=False, timeout=1500)
    report: dict = {"repository": repository, "source_sha": source, "previous_sha": previous,
                    "snapshot_suite": {"exit_code": preflight.returncode, "stdout": preflight.stdout[-2000:]}}
    if preflight.returncode != 0:
        raise ValueError(f"The snapshot's own suite fails before pushing: {preflight.stdout[-1500:]}")
    lease = [f"--force-with-lease=refs/heads/main:{previous}"] if previous else []
    git(repo, "push", "-q", *lease, "origin", "main:refs/heads/main")
    git(repo, "fetch", "-q", "origin")
    git(repo, "branch", "-q", "-u", "origin/main")
    baseline = git(repo, "rev-parse", "HEAD")
    report["baseline_sha"] = baseline
    report["issues"] = ensure_issues(repository, suite)
    json_file(artifact / "seed.json", report)
    wait_baseline_ci(repository, baseline, artifact, target.check)
    report["passed"] = True
    return report
