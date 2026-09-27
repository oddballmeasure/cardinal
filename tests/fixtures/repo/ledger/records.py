"""Read transaction records from a JSON input file."""

import json
from pathlib import Path


def load_transactions(path: Path) -> list[dict]:
    return json.loads(path.read_text())
