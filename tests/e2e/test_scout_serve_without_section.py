import json
import sys
from pathlib import Path

from cardinal_harness.product import run_bounded
from test_duplicate_repo_slug import HEADER, repo_block

ROOT = Path(__file__).resolve().parents[2]


def test_scout_serve_without_scout_section_exits_2_before_sleeping(tmp_path):
    home = tmp_path / "cardinal-home"
    home.mkdir()
    (home / "cardinal.toml").write_text(HEADER + repo_block("owner/name", "true"))

    cli = [sys.executable, "-m", "cardinal.cli.main", "--home", str(home)]
    result = run_bounded([*cli, "scout", "--repo", "owner/name", "--interval", "1"], timeout=10, cwd=ROOT)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert json.loads(result.stderr.strip()) == {"error": "No [scout] section in cardinal.toml"}, result.stderr

    stored = run_bounded([*cli, "logs", "query"], timeout=30, cwd=ROOT)
    assert stored.returncode == 0, (stored.returncode, stored.stdout, stored.stderr)
    events = [json.loads(line)["record"].get("event") for line in stored.stdout.splitlines() if line.strip()]
    assert "scout_next_pass" not in events, events
