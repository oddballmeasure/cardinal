import sys
from pathlib import Path

import pytest

from cardinal.cli.main import parser
from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]


def run_cli(home, *args):
    return run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home), *args],
                       timeout=30, cwd=ROOT)


@pytest.mark.parametrize("argv", [
    ["daemon", "--interval", "0"],
    ["daemon", "--interval", "-5"],
    ["monitor", "--interval", "-5"],
    ["monitor", "--interval", "0"],
    ["scout", "--repo", "owner/name", "--interval", "0"],
    ["scout", "--repo", "owner/name", "--interval", "-5"],
    ["daemon", "--interval", "soon"],
])
def test_non_positive_interval_is_refused_at_argument_parsing(tmp_path, argv):
    home = tmp_path / "cardinal-home"
    result = run_cli(home, *argv)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "--interval" in result.stderr, result.stderr
    assert not (home / "store.db").exists()


def test_positive_interval_still_parses():
    assert parser().parse_args(["daemon", "--interval", "1"]).interval == 1
    assert parser().parse_args(["monitor", "--interval", "7"]).interval == 7
    assert parser().parse_args(["scout", "--repo", "owner/name", "--interval", "3600"]).interval == 3600


def test_interval_defaults_are_unchanged():
    assert parser().parse_args(["daemon"]).interval == 60
    assert parser().parse_args(["monitor"]).interval == 300
    assert parser().parse_args(["scout", "--repo", "owner/name"]).interval is None
