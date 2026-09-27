"""Attach the log sink to the root logger once configuration is known."""

import logging
import sqlite3

from cardinal.config.models import Config
from cardinal.home import Home
from cardinal.logs.handler import CardinalHandler
from cardinal.logs.sink import Sink
from cardinal.repo.git import PRODUCT_ROOT


def install(home: Home, config: Config, db: sqlite3.Connection) -> Sink:
    root = logging.getLogger()
    level = logging.getLevelNamesMapping()[config.logging.level.upper()]
    for existing in root.handlers:  # stderr keeps the verbosity chosen with -v
        existing.setLevel(max(existing.level, root.level))
    root.setLevel(min(root.level, level))
    sink = Sink(home.logs, db)
    root.addHandler(CardinalHandler(sink, config.logging.source_repo, PRODUCT_ROOT, level))
    return sink
