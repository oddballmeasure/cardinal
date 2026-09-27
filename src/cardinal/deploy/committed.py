"""Deployment inputs come from the merged commit, never from a working tree."""

import subprocess
from pathlib import Path

from cardinal.contracts.deploy import DeployConfig


def git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True, timeout=30).stdout


def committed_deployment(repo: Path, revision: str) -> tuple[str, bytes, bytes, DeployConfig]:
    present = set(git_bytes(repo, "ls-tree", "-r", "--name-only", revision).decode().splitlines())
    root, nested = {"deploy.sh", "deploy.json"}, {"deploy/deploy.sh", "deploy/deploy.json"}
    if present & root and present & nested:
        raise ValueError("Ambiguous deploy layout: files at both the root and deploy/")
    if present & root == root:
        script_path, config_path = "deploy.sh", "deploy.json"
    elif present & nested == nested:
        script_path, config_path = "deploy/deploy.sh", "deploy/deploy.json"
    else:
        raise ValueError("Invalid deploy layout: expected a co-located deploy.sh and deploy.json pair")
    script = git_bytes(repo, "show", f"{revision}:{script_path}")
    config_bytes = git_bytes(repo, "show", f"{revision}:{config_path}")
    return script_path, script, config_bytes, DeployConfig.model_validate_json(config_bytes)
