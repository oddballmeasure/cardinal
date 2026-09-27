"""Live-suite preparation: credentials, the pinned baseline's CI, and the suite's live issues."""

import json
import os
import subprocess
import time
from pathlib import Path

from cardinal_harness.contracts import Issue
from cardinal_harness.product import ROOT, json_file


def prepare_test_repo(url: str, destination: Path) -> dict[str, str]:
    """Clone the live test repository before grading, preserving an existing checkout."""
    if (destination / ".git").is_dir():
        origin = subprocess.run(
            ["git", "-C", str(destination), "remote", "get-url", "origin"],
            text=True, capture_output=True, check=True, timeout=30,
        ).stdout.strip()
        if origin.rstrip("/").removesuffix(".git") != url.rstrip("/").removesuffix(".git"):
            raise ValueError(f"{destination} has origin {origin}, expected {url}")
        status = "existing"
    else:
        subprocess.run(["git", "clone", "--", url, str(destination)],
                       text=True, capture_output=True, check=True, timeout=300)
        status = "cloned"
    revision = subprocess.run(
        ["git", "-C", str(destination), "rev-parse", "HEAD"],
        text=True, capture_output=True, check=True, timeout=30,
    ).stdout.strip()
    return {"path": str(destination), "origin": url, "revision": revision, "status": status}


def gh(*args: str, timeout: int = 60) -> str:
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=True, timeout=timeout).stdout


def load_model_key() -> None:
    """The harness reads .env; the product only ever sees the process environment."""
    if os.environ.get("OPENAI_API_KEY"):
        return
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("OPENAI_API_KEY="):
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("The live suite requires OPENAI_API_KEY")


def use_github_cli_git_credentials() -> None:
    """Use the authenticated gh token for this process's Git operations."""
    os.environ.update({
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "!gh auth git-credential",
    })


def issue_view(repository: str, number: int) -> dict:
    return json.loads(gh("issue", "view", str(number), "-R", repository, "--json", "number,title,body,labels,state"))


def discover_cases(repository: str, cases: list[dict], ready_label: str) -> list[dict]:
    """Match each suite case to exactly one open, ready issue whose body equals its fixture."""
    visible = json.loads(gh("issue", "list", "-R", repository, "--state", "open", "--label", ready_label,
                            "--limit", "100", "--json", "number,title,labels,state"))
    discovered = []
    for case in cases:
        template = Issue.model_validate_json((ROOT / case["issue_fixture"]).read_text())
        difficulty = f"cardinal:difficulty/{case.get('difficulty', case['key'])}"
        matches = [item for item in visible if item["title"] == template.title
                   and {ready_label, difficulty} <= {label["name"] for label in item["labels"]}]
        if len(matches) != 1:
            raise ValueError(f"Expected one ready {case['key']} issue; found {len(matches)}")
        issue = issue_view(repository, matches[0]["number"])
        if issue["state"] != "OPEN" or issue["body"] != template.body:
            raise ValueError(f"Live issue differs from the {case['key']} fixture")
        discovered.append({**case, "issue": issue, "original_labels": [label["name"] for label in issue["labels"]]})
    return discovered


def wait_baseline_ci(repository: str, sha: str, artifact: Path, check: str = "http-e2e") -> None:
    observations = []
    deadline = time.monotonic() + 900
    while True:
        runs = [item for item in json.loads(gh("api", f"repos/{repository}/commits/{sha}/check-runs"))["check_runs"]
                if item["name"] == check and item["head_sha"] == sha]
        latest = max(runs, key=lambda item: item["id"]) if runs else None
        state = "missing" if latest is None else "pending" if latest["status"] != "completed" else latest["conclusion"]
        observations.append(state)
        json_file(artifact / "baseline_ci.json", observations)
        if state == "success":
            return
        if state not in {"missing", "pending"}:
            raise ValueError(f"Baseline CI failed: {state}")
        if time.monotonic() >= deadline:
            raise ValueError("Baseline CI did not report success")
        time.sleep(10)
