"""The monitor's first/last seen span the group's records by time, whatever order they arrived in."""

from datetime import UTC, datetime, timedelta

from cardinal.config.models import Monitor
from cardinal.logs.record import LogRecord, Source
from cardinal.logs.sink import Sink
from cardinal.monitor.scan import fill, new_groups
from cardinal.store.db import connect

SETTINGS = Monitor(min_occurrences=1, window_hours=24, max_issues_per_pass=5)


def sent(at: datetime) -> LogRecord:
    return LogRecord(at=at, level="error", event="late_retry", message="late_retry",
                     source=Source(repo="example/app", component="api"))


def stored_at(db, record) -> str:
    return db.execute("SELECT at FROM logs WHERE id = ?", (record.id,)).fetchone()["at"]


def test_first_and_last_seen_follow_time_not_ingest_order(tmp_path):
    db = connect(tmp_path / "store.db")
    sink = Sink(tmp_path / "logs", db)
    now = datetime.now(UTC)

    later = sink.write(sent(now - timedelta(hours=1)))
    earlier = sink.write(sent(now - timedelta(hours=2)))
    assert later.fingerprint == earlier.fingerprint

    groups, _ = new_groups(db, 0)
    group = fill(db, next(g for g in groups if g.fingerprint == later.fingerprint), SETTINGS)

    assert group.count == 2
    assert group.first_seen == stored_at(db, earlier), "first_seen must be the earliest record's time"
    assert group.last_seen == stored_at(db, later), "last_seen must be the latest record's time"
    assert group.sample["id"] == later.id, "the sample must be the latest record by time, not the last ingested"
    assert group.sample["id"] != earlier.id
