import json
import os
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cardinal.config.models import Config
from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]

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
bind = "{bind}"
token_env = "CARDINAL_INGEST_TOKEN"

[[repos]]
slug = "example/app"
base_branch = "main"
test_command = ["true"]
required_checks = []
paths_off_limits = []
paths_hidden = []
"""


def test_ingest_bind_port_above_65535_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        Config.model_validate(tomllib.loads(CONFIG.format(bind="127.0.0.1:99999")))
    assert Config.model_validate(tomllib.loads(CONFIG.format(bind="127.0.0.1:65535"))).ingest.bind == "127.0.0.1:65535"

    home = tmp_path / "home"
    home.mkdir()
    (home / "cardinal.toml").write_text(CONFIG.format(bind="127.0.0.1:99999"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("CARDINAL_")}
    env["CARDINAL_INGEST_TOKEN"] = "token"
    result = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), "ingest"],
                         timeout=30, cwd=ROOT, env=env)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr[-2000:])
    assert "Traceback" not in result.stderr, result.stderr[-2000:]
    last = result.stderr.strip().splitlines()[-1]
    assert "error" in json.loads(last), result.stderr[-2000:]
