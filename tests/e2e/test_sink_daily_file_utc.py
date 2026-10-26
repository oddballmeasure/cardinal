"""The daily JSONL archive is named after the record's UTC date, the same day its `logs.at` shows."""

from datetime import datetime

from cardinal.logs.record import LogRecord, Source
from cardinal.logs.sink import Sink
from cardinal.store.db import connect


def sent(at: datetime, event: str) -> LogRecord:
    return LogRecord(at=at, level="error", event=event, message=event,
                     source=Source(repo="example/app", component="api"))


def test_daily_archive_file_uses_utc_date(tmp_path):
    db = connect(tmp_path / "store.db")
    logs = tmp_path / "logs"
    sink = Sink(logs, db)

    offset = sink.write(sent(datetime.fromisoformat("2026-10-22T22:00:00-05:00"), "offset_record"))
    stored = db.execute("SELECT at FROM logs WHERE id = ?", (offset.id,)).fetchone()["at"]
    assert stored == "2026-10-23T03:00:00+00:00"

    utc_day = logs / "2026-10-23.jsonl"
    assert utc_day.exists(), f"no UTC-dated archive; files: {sorted(p.name for p in logs.iterdir())}"
    assert len(utc_day.read_text(encoding="utf-8").splitlines()) == 1
    assert not (logs / "2026-10-22.jsonl").exists(), "record was archived under the sender's local date"

    naive = sink.write(sent(datetime.fromisoformat("2026-10-22T22:00:00"), "naive_record"))
    local_day = logs / "2026-10-22.jsonl"
    assert local_day.exists()
    assert naive.id in local_day.read_text(encoding="utf-8")
