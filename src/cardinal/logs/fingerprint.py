"""Group records that describe the same defect. Line numbers are left out so an unrelated edit
above a failing line does not split one defect into two."""

import hashlib

from cardinal.logs.record import LogRecord

FRAMES = 5


def fingerprint(record: LogRecord) -> str:
    parts = [record.source.repo]
    if record.error and record.error.stack:
        own = [frame for frame in record.error.stack if not library(frame.file)] or record.error.stack
        frames = own[-FRAMES:]
        parts += [record.error.type, *(f"{frame.file}:{frame.function}" for frame in frames)]
    else:
        parts += [record.source.component, record.event, record.failure_kind or "",
                  record.error.type if record.error else ""]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:16]


def library(path: str) -> bool:
    """Frames in installed packages or the interpreter say where a defect surfaced, not where it is."""
    return path.startswith("<") or "site-packages" in path or "/lib/python" in path


def stamped(record: LogRecord) -> LogRecord:
    return record.model_copy(update={"fingerprint": fingerprint(record)})
