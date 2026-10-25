"""`cardinal repos check` probes focused_test_command's executable when it is set (issue #46)."""

import json
import logging
import os
import signal
import sys
from pathlib import Path

import pytest

from cardinal.cli import commands
from cardinal.cli.main import main
from cardinal.config.load import load
from cardinal.home import Home

HEADER = """\
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
"""


def write_home(tmp_path, focused=None) -> Home:
    home = tmp_path / "cardinal-home"
    home.mkdir(parents=True)
    focused_line = f"focused_test_command = {json.dumps(focused)}\n" if focused is not None else ""
    (home / "cardinal.toml").write_text(HEADER + f"""
[[repos]]
slug = "owner/name"
base_branch = "main"
test_command = ["{Path(sys.executable).name}"]
{focused_line}required_checks = []
paths_off_limits = []
paths_hidden = []
""")
    return Home(home)


@pytest.fixture(autouse=True)
def other_probes_pass(monkeypatch):
    monkeypatch.setattr(commands, "gh", lambda *args, **kwargs: "Logged in")
    monkeypatch.setattr(commands, "gh_json", lambda *args, **kwargs: {"nameWithOwner": "owner/name"})
    monkeypatch.setattr(commands, "git", lambda *args, **kwargs: "0123456789abcdef0123\trefs/heads/main\n")
    monkeypatch.setattr(commands.issues, "ensure_labels", lambda slug, labels: None)
    monkeypatch.setenv("CARDINAL_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""))


@pytest.fixture(autouse=True)
def restore_process_state():
    """The in-process CLI attaches a log sink to the root logger and a SIGTERM handler; undo both."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    sigterm = signal.getsignal(signal.SIGTERM)
    yield
    for handler in root.handlers[:]:
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    signal.signal(signal.SIGTERM, sigterm)


def result_for(tmp_path, focused=None) -> dict:
    home = write_home(tmp_path, focused)
    config = load(home)
    return commands.check_result(home, config, config.repo("owner/name"))


def test_missing_focused_runner_fails_the_check(tmp_path):
    result = result_for(tmp_path, ["no-such-runner-xyz", "{test_path}"])
    assert result["checks"]["focused test command"] == "FAILED no-such-runner-xyz is not on PATH"
    assert result["checks"]["test command"].startswith("ok")
    assert result["ok"] is False


def test_focused_runner_on_path_passes(tmp_path):
    runner = Path(sys.executable).name
    result = result_for(tmp_path, [runner, "{test_path}"])
    assert result["checks"]["focused test command"].startswith("ok"), result
    assert result["ok"] is True, result


def test_unset_focused_command_adds_no_probe(tmp_path):
    result = result_for(tmp_path)
    assert "focused test command" not in result["checks"]
    assert result["ok"] is True, result


def test_cli_repos_check_exits_1_when_only_focused_probe_fails(tmp_path, capsys):
    home = write_home(tmp_path, ["no-such-runner-xyz", "{test_path}"])
    code = main(["--home", str(home.root), "repos", "check", "--repo", "owner/name"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1, report
    failed = {name: value for name, value in report["checks"].items() if value.startswith("FAILED")}
    assert failed == {"focused test command": "FAILED no-such-runner-xyz is not on PATH"}
    assert report["ok"] is False


def test_cli_repos_check_exits_0_when_focused_runner_exists(tmp_path, capsys):
    home = write_home(tmp_path, [Path(sys.executable).name, "{test_path}"])
    code = main(["--home", str(home.root), "repos", "check", "--repo", "owner/name"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report
    assert report["checks"]["focused test command"].startswith("ok")
