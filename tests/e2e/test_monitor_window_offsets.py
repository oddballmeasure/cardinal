"""The monitor's window compares record times as instants, whatever UTC offset the sender used."""

from datetime import UTC, datetime, timedelta, timezone

from cardinal.config.models import Monitor
from cardinal.logs.record import LogRecord, Source
from cardinal.logs.sink import Sink
from cardinal.monitor.scan import fill, new_groups
from cardinal.store.db import connect

SETTINGS = Monitor(min_occurrences=1, window_hours=24, max_issues_per_pass=5)


def sent(at: datetime, event: str) -> LogRecord:
    return LogRecord(at=at, level="error", event=event, message=event,
                     source=Source(repo="example/app", component="api"))


def counted(db, record: LogRecord) -> int:
    groups, _ = new_groups(db, 0)
    group = next(g for g in groups if g.fingerprint == record.fingerprint)
    return fill(db, group, SETTINGS).count


def test_window_counts_records_by_instant_not_by_offset_text(tmp_path):
    db = connect(tmp_path / "store.db")
    sink = Sink(tmp_path / "logs", db)
    now = datetime.now(UTC)

    inside = sink.write(sent((now - timedelta(hours=20)).astimezone(timezone(timedelta(hours=-7))), "inside_window"))
    outside = sink.write(sent((now - timedelta(hours=30)).astimezone(timezone(timedelta(hours=8))), "outside_window"))
    local = sink.write(sent(datetime.fromisoformat("2026-10-16T03:00:00-07:00"), "stored_value"))

    assert counted(db, inside) == 1, "a record 20h ago sent with -07:00 is inside a 24h window"
    assert counted(db, outside) == 0, "a record 30h ago sent with +08:00 is outside a 24h window"
    stored = db.execute("SELECT at FROM logs WHERE id = ?", (local.id,)).fetchone()["at"]
    assert stored == "2026-10-16T10:00:00+00:00"
