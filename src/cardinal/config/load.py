"""Read cardinal.toml from the home directory."""

import tomllib

from cardinal.config.models import Config
from cardinal.home import Home


def load(home: Home) -> Config:
    if not home.config.is_file():
        raise FileNotFoundError(f"No configuration at {home.config}; run `cardinal init` first")
    return Config.model_validate(tomllib.loads(home.config.read_text()))


TEMPLATE = """\
# Cardinal configuration. Every repository decision is explicit; nothing defaults open.

[models]
orchestrator = "openai:gpt-6-sol"
profiler = "openai:gpt-6-sol"
coder = "openai:gpt-6-sol"
verifier = "openai:gpt-6-sol"
pr_manager = "openai:gpt-6-sol"
deployer = "openai:gpt-6-sol"

[[repos]]
slug = "{slug}"
base_branch = "main"
test_command = ["python", "-m", "pytest", "-q", "tests"]
required_checks = []
paths_off_limits = [".github/"]
"""
