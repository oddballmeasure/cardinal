"""Repository E2E checks contributed with the completed issue in replay."""

import csv
import io
import json
import subprocess
import sys


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ledger", "--input", "data/transactions.json", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_default_json_remains_available() -> None:
    result = run_cli()
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 3


def test_date_filter_is_inclusive() -> None:
    result = run_cli("--since", "2026-02-03")
    assert result.returncode == 0, result.stderr
    assert [item["id"] for item in json.loads(result.stdout)] == ["t-002", "t-003"]


def test_csv_filter_preserves_unicode() -> None:
    result = run_cli("--since", "2026-02-03", "--format", "csv")
    assert result.returncode == 0, result.stderr
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [item["merchant"] for item in rows] == ["Café", "Books"]
