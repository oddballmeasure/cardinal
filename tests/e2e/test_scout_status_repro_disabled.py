import json
import sys
from contextlib import closing
from pathlib import Path

from cardinal.store.db import connect
from cardinal_harness.product import run_bounded
from test_duplicate_repo_slug import HEADER, repo_block
from test_scout_status_zero_done import file_proposal

ROOT = Path(__file__).resolve().parents[2]

SCOUT = """
[scout]
autonomy = "auto"
categories = ["bug", "feature"]
areas_per_pass = 1
cooldown_days = 0
max_proposals_per_pass = 1
max_files = 1
repro = {repro}
auto_min_decided = 1
auto_min_approval = 0.8
auto_min_done = 0
"""


def scout_status(tmp_path, repro: str) -> dict:
    home = tmp_path / f"cardinal-home-{repro}"
    home.mkdir()
    models = HEADER.replace('monitor = "openai:unused"\n',
                            'monitor = "openai:unused"\nscout = "openai:unused"\nscout_reviewer = "openai:unused"\n')
    (home / "cardinal.toml").write_text(models + SCOUT.format(repro=repro) + repo_block("a/x", "true"))
    with closing(connect(home / "store.db")) as db:
        file_proposal(db, 1, "bug", "approved")
        file_proposal(db, 2, "bug", "approved")
        file_proposal(db, 3, "feature", "approved")
        file_proposal(db, 4, "feature", "approved")
        db.commit()
    result = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home),
                          "scout", "status", "--repo", "a/x"], timeout=30, cwd=ROOT)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_scout_status_with_repro_disabled_reports_bug_gate_proposed(tmp_path):
    off = scout_status(tmp_path, "false")
    bug = off["categories"]["bug"]
    assert bug["gate"] == "proposed", bug
    assert "no test that fails on the base branch" in bug["why"], bug
    assert off["categories"]["feature"]["gate"] == "auto", off["categories"]["feature"]

    on = scout_status(tmp_path, "true")
    assert on["categories"]["bug"]["gate"] == "auto", on["categories"]["bug"]
