"""Bridge the standard `logging` module into LogRecords, so every `log.*` call in Cardinal and its
libraries reaches the sink. Structured fields ride in `extra`: event, failure_kind, data."""

import logging
import os
import socket
import subprocess
import traceback

from cardinal.logs.context import CURRENT
from cardinal.logs.record import Context, Error, Frame, LogRecord, Source
from cardinal.logs.sink import Sink

LEVEL_NAMES = {logging.DEBUG: "debug", logging.INFO: "info", logging.WARNING: "warning",
               logging.ERROR: "error", logging.CRITICAL: "critical"}


def revision(root) -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True,
                              check=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None  # an installed Cardinal has no checkout; the record simply carries no revision


def error_of(exc: BaseException, root: str) -> Error:
    """Paths are made relative to the checkout: fingerprints then match across installs, and a
    traceback copied into a public issue does not reveal the host's directories."""
    frames = traceback.extract_tb(exc.__traceback__)
    text = "".join(traceback.format_exception(exc)).replace(root, "")
    return Error(type=type(exc).__name__, message=str(exc)[:4000],
                 stack=[Frame(file=frame.filename.removeprefix(root), function=frame.name, line=frame.lineno)
                        for frame in frames],
                 traceback=text[-20000:])


class CardinalHandler(logging.Handler):
    def __init__(self, sink: Sink, repo: str, root, level: int) -> None:
        super().__init__(level)
        self.sink = sink
        self.root = f"{root}/"
        self.source = {"repo": repo, "revision": revision(root), "host": socket.gethostname(), "pid": os.getpid()}

    def emit(self, entry: logging.LogRecord) -> None:
        try:
            exc = entry.exc_info[1] if entry.exc_info else None
            context = CURRENT.get()
            self.sink.write(LogRecord(
                level=LEVEL_NAMES.get(entry.levelno, "error" if entry.levelno > logging.ERROR else "info"),
                event=getattr(entry, "event", "exception" if exc else "log"),
                message=entry.getMessage()[:8000],
                source=Source(component=entry.name, **self.source),
                context=Context(**{key: context.get(key) for key in Context.model_fields}),
                failure_kind=getattr(entry, "failure_kind", None),
                error=error_of(exc, self.root) if exc else None,
                data=getattr(entry, "data", {}),
            ))
        except Exception:  # noqa: BLE001 - logging.Handler contract: report on stderr, never raise
            self.handleError(entry)
