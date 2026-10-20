import json
import sys
from contextlib import closing
from pathlib import Path

from cardinal.store.db import connect
from cardinal_harness.product import run_bounded
from test_duplicate_repo_slug import HEADER, repo_block

ROOT = Path(__file__).resolve().parents[2]


def run_status(home, *args):
    result = run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), "status", *args],
                         timeout=30, cwd=ROOT)
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return json.loads(result.stdout)


def test_status_summary_rows_name_their_repo(tmp_path):
    home = tmp_path / "cardinal-home"
    home.mkdir()
    (home / "cardinal.toml").write_text(HEADER + repo_block("a/x", "true") + repo_block("b/y", "true"))
    with closing(connect(home / "store.db")) as db:
        for run_id, slug, started in (("run-a", "a/x", "2025-01-01T00:00:00+00:00"),
                                      ("run-b", "b/y", "2025-01-01T00:01:00+00:00")):
            db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, started_at)"
                       " VALUES (?, ?, ?, 42, 'done', ?)", (run_id, run_id, slug, started))
        db.commit()

    rows = run_status(home, "42")
    assert len(rows) == 2, rows
    assert all("repo" in row for row in rows), rows
    assert [row["repo"] for row in rows] == ["a/x", "b/y"]

    filtered = run_status(home, "42", "--repo", "a/x")
    assert len(filtered) == 1 and filtered[0]["repo"] == "a/x", filtered
