import json
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from cardinal.store.db import connect
from cardinal_harness.product import run_bounded
from test_duplicate_repo_slug import HEADER, repo_block

ROOT = Path(__file__).resolve().parents[2]

SCOUT = """
[scout]
autonomy = "auto"
categories = ["feature", "enhancement"]
areas_per_pass = 1
cooldown_days = 0
max_proposals_per_pass = 1
max_files = 1
repro = false
auto_min_decided = 1
auto_min_approval = 0.8
auto_min_done = 0
"""


def file_proposal(db, number, category, outcome):
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    db.execute("INSERT INTO scout_proposals (repo, pass_id, fingerprint, category, title, proposal, status,"
               " filed_mode, issue, outcome, created_at, decided_at)"
               " VALUES ('a/x', 'pass-1', ?, ?, ?, '{}', 'filed', 'proposed', ?, ?, ?, ?)",
               (f"fp-{number}", category, f"Proposal {number}", number, outcome, stamp, stamp))


def test_scout_status_with_zero_min_done_and_no_landed_issues_reports_auto(tmp_path):
    home = tmp_path / "cardinal-home"
    home.mkdir()
    models = HEADER.replace('monitor = "openai:unused"\n',
                            'monitor = "openai:unused"\nscout = "openai:unused"\nscout_reviewer = "openai:unused"\n')
    assert "scout_reviewer" in models
    (home / "cardinal.toml").write_text(models + SCOUT + repo_block("a/x", "true"))
    with closing(connect(home / "store.db")) as db:
        file_proposal(db, 1, "feature", "approved")  # decided, not landed yet
        file_proposal(db, 2, "feature", "approved")
        file_proposal(db, 3, "enhancement", "done")  # landed: one done, one error
        file_proposal(db, 4, "enhancement", "error")
        db.commit()

    result = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home),
                          "scout", "status", "--repo", "a/x"], timeout=30, cwd=ROOT)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    assert "TypeError" not in result.stderr, result.stderr
    report = json.loads(result.stdout)

    feature = report["categories"]["feature"]
    assert feature["done_rate"] is None, feature
    assert feature["gate"] == "auto", feature
    assert "no landed issues" in feature["why"], feature
    assert "None" not in feature["why"], feature

    enhancement = report["categories"]["enhancement"]
    assert enhancement["gate"] == "auto", enhancement
    assert "50% of landed ones done" in enhancement["why"], enhancement
