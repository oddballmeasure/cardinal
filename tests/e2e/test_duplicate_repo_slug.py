import pytest
from pydantic import ValidationError

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


def repo_block(slug, command):
    return f"""
[[repos]]
slug = "{slug}"
base_branch = "main"
test_command = ["{command}"]
required_checks = []
paths_off_limits = []
paths_hidden = []
"""


def write_config(tmp_path, text):
    home = tmp_path / "cardinal-home"
    home.mkdir(parents=True)
    (home / "cardinal.toml").write_text(text)
    return Home(home)


def test_config_rejects_repeated_repo_slug_and_keeps_distinct_slugs(tmp_path):
    duplicate = write_config(tmp_path / "dup",
                             HEADER + repo_block("owner/name", "true") + repo_block("owner/name", "false"))
    with pytest.raises(ValidationError) as raised:
        load(duplicate)
    assert "owner/name" in str(raised.value)

    distinct = write_config(tmp_path / "ok",
                            HEADER + repo_block("owner/name", "true") + repo_block("owner/other", "false"))
    config = load(distinct)
    assert config.repo("owner/other").test_command == ["false"]
    assert config.repo("owner/name").test_command == ["true"]
