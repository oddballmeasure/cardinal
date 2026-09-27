"""Black-box checks for issue 104; intentionally outside the writable fixture repo."""

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path


TARGET_REPO = Path(os.environ["CARDINAL_TARGET_REPO"])


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ledger", "--input", "data/transactions.json", *args],
        cwd=TARGET_REPO,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_since_is_inclusive() -> None:
    result = run_cli("--since", "2026-02-03")
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert [row["id"] for row in rows] == ["t-002", "t-003"]


def test_invalid_since_is_rejected() -> None:
    result = run_cli("--since", "2026-02-30")
    assert result.returncode != 0
    assert "2026-02-30" in result.stderr


def test_csv_preserves_quoting_and_unicode() -> None:
    result = run_cli("--format", "csv")
    assert result.returncode == 0, result.stderr
    assert '"Office, East"' in result.stdout
    assert "Café" in result.stdout
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [row["id"] for row in rows] == ["t-001", "t-002", "t-003"]
    assert rows[0]["merchant"] == "Office, East"


def test_empty_csv_keeps_header() -> None:
    result = run_cli("--since", "2026-03-01", "--format", "csv")
    assert result.returncode == 0, result.stderr
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert rows == [["id", "date", "merchant", "amount"]]


def test_since_and_csv_combine() -> None:
    result = run_cli("--since", "2026-02-03", "--format", "csv")
    assert result.returncode == 0, result.stderr
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert [row["id"] for row in rows] == ["t-002", "t-003"]
