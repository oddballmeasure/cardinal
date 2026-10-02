"""Drive a Cardinal checkout's CLI from outside: a temporary home, its own `cardinal ingest`, and
records seeded the way a running app would send them."""

import json
import os
import subprocess
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

TOKEN = "acceptance-token"
CONFIG = """\
[models]
orchestrator = "openai:unused"
profiler = "openai:unused"
coder = "openai:unused"
verifier = "openai:unused"
pr_manager = "openai:unused"
deployer = "openai:unused"
monitor = "openai:unused"

[logging]
level = "info"
source_repo = "example/cardinal"

[ingest]
bind = "127.0.0.1:0"
token_env = "CARDINAL_INGEST_TOKEN"

[[repos]]
slug = "example/app"
base_branch = "main"
test_command = ["true"]
required_checks = []
paths_off_limits = []

[[repos]]
slug = "example/other"
base_branch = "main"
test_command = ["true"]
required_checks = []
paths_off_limits = []
"""


def environment() -> dict:
    # The checkout runs in its own environment, never this interpreter's.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("CARDINAL_") and key not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    return {**env, "CARDINAL_INGEST_TOKEN": TOKEN}


class Cardinal:
    def __init__(self, checkout: Path, home: Path) -> None:
        self.base = ["uv", "run", "--project", str(checkout), "--locked", "python", "-m", "cardinal.cli.main",
                     "--home", str(home)]
        self.home = home

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([*self.base, *args], env=environment(), text=True, capture_output=True,
                              check=False, timeout=300)


@pytest.fixture
def cardinal(tmp_path: Path) -> Cardinal:
    home = tmp_path / "home"
    home.mkdir()
    (home / "cardinal.toml").write_text(CONFIG)
    return Cardinal(Path(os.environ["CARDINAL_TARGET_REPO"]).resolve(), home)


def record(days_ago: int = 0, level: str = "error", repo: str = "example/app", event: str = "request_failed") -> dict:
    return {"schema_version": 1, "id": uuid.uuid4().hex,
            "at": (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
            "level": level, "event": event, "message": f"{event} {level}",
            "source": {"repo": repo, "component": "api"}}


@pytest.fixture
def send(cardinal: Cardinal):
    """Start the checkout's ingest endpoint; yields a function that posts records to it."""
    process = subprocess.Popen([*cardinal.base, "ingest"], env=environment(), text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = process.stdout.readline()
    assert line, process.stderr.read()[-2000:]
    url = json.loads(line)["listening"]

    def post(records: list[dict]) -> None:
        request = urllib.request.Request(url, data=json.dumps(records).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {TOKEN}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert json.loads(response.read())["accepted"] == len(records)

    try:
        yield post
    finally:
        process.terminate()
        process.wait(timeout=30)
