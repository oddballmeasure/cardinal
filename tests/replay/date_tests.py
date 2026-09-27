"""Repository E2E checks contributed with the date ticket in replay."""

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


def test_since_includes_boundary_and_later() -> None:
    result = run_cli("--since", "2026-02-03")
    assert result.returncode == 0, result.stderr
    assert [item["id"] for item in json.loads(result.stdout)] == ["t-002", "t-003"]


def test_impossible_date_is_rejected() -> None:
    result = run_cli("--since", "2026-02-30")
    assert result.returncode != 0
    assert "2026-02-30" in result.stderr
