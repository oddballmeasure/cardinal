"""Render active transaction output in JSON or RFC-style CSV."""

import csv
import io
import json


FIELDS = ["id", "date", "merchant", "amount"]


def render_json(rows: list[dict]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def render_csv(rows: list[dict]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
