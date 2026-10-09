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
monitor = "openai:gpt-6-sol"
# scout = "openai:gpt-6-sol"            # required with [scout]
# scout_reviewer = "openai:gpt-6-sol"   # required with [scout]

[logging]
level = "info"
source_repo = "oddballmeasure/cardinal"  # Cardinal's own defects are filed here

# Optional: `cardinal ingest` accepts LogRecords from running apps (see `cardinal logs schema`).
# [ingest]
# bind = "127.0.0.1:8787"
# token_env = "CARDINAL_INGEST_TOKEN"

# Optional: `cardinal monitor` files issues for repeated errors.
# [monitor]
# min_occurrences = 3
# window_hours = 24
# max_issues_per_pass = 3

# Optional: `cardinal scout --repo X` proposes small, reviewed issues as cardinal:proposed.
# [scout]
# autonomy = "propose"          # or "auto": a category with a good track record files ready
# categories = ["bug", "feature"]
# areas_per_pass = 2
# cooldown_days = 7
# max_proposals_per_pass = 3
# max_files = 3
# repro = true                  # bugs get a test that must fail on the base branch
# auto_min_decided = 10
# auto_min_approval = 0.8
# auto_min_done = 0.7

[[repos]]
slug = "{slug}"
base_branch = "main"
test_command = ["python", "-m", "pytest", "-q", "tests"]
required_checks = []
paths_off_limits = [".github/"]
paths_hidden = []
# max_parallel_runs = 1  # issues the daemon works at once; more runs mean more base syncs, so
#                        # consider raising [limits] base_sync_rounds with it
"""
