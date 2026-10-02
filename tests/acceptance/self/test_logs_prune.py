"""Independent checks for `cardinal logs prune --days N`."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from conftest import record


def day(days_ago: int) -> str:
    return f"{datetime.now(UTC) - timedelta(days=days_ago):%Y-%m-%d}.jsonl"


def stored_ids(home) -> set[str]:
    with sqlite3.connect(home / "store.db") as db:
        return {row[0] for row in db.execute("SELECT id FROM logs")}


def seeded(send):
    old = [record(40), record(40, event="other_failure")]
    kept = [record(10), record(0)]
    send(old + kept)
    return old, kept


def test_prune_removes_old_log_files_and_keeps_recent_ones(cardinal, send) -> None:
    seeded(send)
    logs = cardinal.home / "logs"
    assert (logs / day(40)).exists() and (logs / day(10)).exists()
    result = cardinal("logs", "prune", "--days", "30")
    assert result.returncode == 0, result.stderr
    assert not (logs / day(40)).exists()
    assert (logs / day(10)).exists() and (logs / day(0)).exists()


def test_prune_removes_old_store_rows_and_keeps_newer_ones(cardinal, send) -> None:
    old, kept = seeded(send)
    result = cardinal("logs", "prune", "--days", "30")
    assert result.returncode == 0, result.stderr
    remaining = stored_ids(cardinal.home)
    assert not {item["id"] for item in old} & remaining
    assert {item["id"] for item in kept} <= remaining


def test_prune_reports_counts_and_refuses_days_below_one(cardinal, send) -> None:
    seeded(send)
    before = sorted(path.name for path in (cardinal.home / "logs").iterdir())
    refused = cardinal("logs", "prune", "--days", "0")
    assert refused.returncode == 2
    assert "error" in json.loads(refused.stderr.strip().splitlines()[-1])
    assert sorted(path.name for path in (cardinal.home / "logs").iterdir()) == before
    result = cardinal("logs", "prune", "--days", "30")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"files_removed": 1, "records_removed": 2}
