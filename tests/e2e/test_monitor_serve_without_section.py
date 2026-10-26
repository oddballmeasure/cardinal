"""`cardinal monitor` in serve mode with no [monitor] section must fail at once, like `--once` does,
instead of logging a failed pass every interval forever."""

import json
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]


def environment() -> dict:
    path = os.environ.get("PYTHONPATH")
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *([path] if path else [])])}


def cardinal(home: Path, *args: str, timeout: float = 60):
    return run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), *args],
                       timeout=timeout, cwd=ROOT, env=environment())


def test_monitor_serve_without_monitor_section_exits_2(tmp_path):
    home = tmp_path / "cardinal-home"
    init = cardinal(home, "init", "--repo", "team/app")
    assert init.returncode == 0, init.stderr
    assert "monitor" not in tomllib.loads((home / "cardinal.toml").read_text())

    result = cardinal(home, "monitor", "--interval", "1", timeout=15)

    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr[-3000:])
    last = result.stderr.strip().splitlines()[-1]
    assert json.loads(last) == {"error": "No [monitor] section in cardinal.toml"}, result.stderr[-3000:]
    with sqlite3.connect(home / "store.db") as db:
        failed = db.execute("SELECT COUNT(*) FROM logs WHERE event = 'monitor_pass_failed'").fetchone()[0]
    assert failed == 0, f"{failed} monitor_pass_failed records were logged"
