"""The one log record every Cardinal part writes and every monitored app sends.

`cardinal logs schema` prints this model as JSON Schema; that output is the contract other
repositories write against. Bump `schema_version` for any change a sender would notice.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cardinal.home import SLUG

Level = Literal["debug", "info", "warning", "error", "critical"]
LEVELS: list[str] = ["debug", "info", "warning", "error", "critical"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(Strict):
    repo: str = Field(pattern=SLUG.pattern, description="owner/name of the repository whose code emitted this")
    component: str = Field(min_length=1, description="Module, service or process name, e.g. cardinal.graph.nodes or api")
    revision: str | None = Field(default=None, description="Git sha the emitting code was built from")
    host: str | None = None
    pid: int | None = None


class Context(Strict):
    run_id: str | None = None
    issue: int | None = None
    stage: str | None = None
    ticket_id: str | None = None


class Frame(Strict):
    file: str
    function: str
    line: int | None = None


class Error(Strict):
    type: str = Field(min_length=1, description="Exception class name, e.g. KeyError")
    message: str
    stack: list[Frame] = Field(default_factory=list, description="Outermost first, as Python prints tracebacks")
    traceback: str | None = Field(default=None, max_length=20000)


class LogRecord(Strict):
    schema_version: Literal[1] = 1
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    level: Level
    event: str = Field(pattern=r"^[a-z][a-z0-9_]*$", description="Stable snake_case name, e.g. stage_failed")
    message: str
    source: Source
    context: Context = Field(default_factory=Context)
    failure_kind: str | None = None
    error: Error | None = None
    data: dict = Field(default_factory=dict)
    fingerprint: str | None = Field(default=None, description="Set by Cardinal on receipt; a sender's value is replaced")
