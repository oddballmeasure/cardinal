"""Active JSON presentation for transaction records."""

import json


def render_json(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False)
