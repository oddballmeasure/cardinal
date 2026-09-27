"""Telemetry. A failed write is logged and dropped: losing a record must never lose a paid-for run."""

import functools
import json
import logging
import sqlite3

from cardinal.store.db import now

log = logging.getLogger(__name__)


def safe(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - see module docstring; logged, never silent
            log.exception("recorder.%s failed; the run continues without this record", method.__name__)
            return None
    return wrapper


class Recorder:
    def __init__(self, db: sqlite3.Connection, run_id: str) -> None:
        self.db = db
        self.run_id = run_id

    @safe
    def event(self, stage: str, kind: str, data: object) -> None:
        self.db.execute(
            "INSERT INTO events (run_id, at, stage, kind, data) VALUES (?, ?, ?, ?, ?)",
            (self.run_id, now(), stage, kind, json.dumps(data, default=str, ensure_ascii=False)),
        )

    @safe
    def agent_call(self, stage: str, ticket_id: str | None, started_at: str, seconds: float,
                   usage: tuple[int, int], tool_calls: list[str], outcome: str, transcript: list) -> None:
        self.db.execute(
            "INSERT INTO agent_calls (run_id, stage, ticket_id, started_at, seconds, input_tokens,"
            " output_tokens, tool_calls, outcome, transcript) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.run_id, stage, ticket_id, started_at, seconds, usage[0], usage[1],
             json.dumps(tool_calls), outcome, json.dumps(transcript, default=str, ensure_ascii=False)),
        )
