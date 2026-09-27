"""Existing smoke check in the fake repository."""

import json
import subprocess
import sys


def test_default_json_output() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ledger", "--input", "data/transactions.json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 3


def test_merchant_selection() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ledger", "--input", "data/transactions.json", "--merchant", "Café"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert [row["id"] for row in json.loads(result.stdout)] == ["t-002"]
