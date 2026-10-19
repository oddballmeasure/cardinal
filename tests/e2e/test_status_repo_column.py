from cardinal.cli.commands import status
from cardinal.store.db import connect


def test_status_summary_rows_name_their_repo(tmp_path):
    db = connect(tmp_path / "store.db")
    for run_id, slug, started in (("run-a", "a/x", "2025-01-01T00:00:00+00:00"),
                                  ("run-b", "b/y", "2025-01-01T00:01:00+00:00")):
        db.execute("INSERT INTO runs (run_id, thread_id, repo, issue, status, started_at)"
                   " VALUES (?, ?, ?, 42, 'done', ?)", (run_id, run_id, slug, started))

    rows = status(db, None, 42, False)
    assert len(rows) == 2
    assert all("repo" in row for row in rows), rows
    assert [row["repo"] for row in rows] == ["a/x", "b/y"]

    filtered = status(db, "a/x", 42, False)
    assert len(filtered) == 1 and filtered[0]["repo"] == "a/x", filtered

    detailed = status(db, None, 42, True)
    assert [row["repo"] for row in detailed] == ["a/x", "b/y"]
    assert all(row["events"] == [] and row["agent_calls"] == [] for row in detailed), detailed
