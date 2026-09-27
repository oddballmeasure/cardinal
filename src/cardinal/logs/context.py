"""Which run and stage the current code is serving, so every record links back without threading
arguments through every call."""

from contextlib import contextmanager
from contextvars import ContextVar

CURRENT: ContextVar[dict] = ContextVar("cardinal_log_context", default={})


@contextmanager
def bind(**fields: object):
    token = CURRENT.set({**CURRENT.get(), **{key: value for key, value in fields.items() if value is not None}})
    try:
        yield
    finally:
        CURRENT.reset(token)
