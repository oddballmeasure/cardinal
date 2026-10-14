import json
import sys
from pathlib import Path

import pytest

from cardinal.config.load import load
from cardinal.home import Home
from cardinal_harness.product import run_bounded

ROOT = Path(__file__).resolve().parents[2]


def run_init(home, slug):
    return run_bounded([sys.executable, "-m", "cardinal.cli.main", "--home", str(home),
                        "init", "--repo", slug], timeout=30, cwd=ROOT)


@pytest.mark.parametrize("slug", ["notaslug", 'a"b/c'])
def test_init_rejects_malformed_repo_slug_without_writing_anything(tmp_path, slug):
    home = tmp_path / "cardinal-home"
    init = run_init(home, slug)
    assert init.returncode == 2, (init.returncode, init.stdout, init.stderr)
    assert "error" in json.loads(init.stderr.strip().splitlines()[-1])
    assert not (home / "cardinal.toml").exists()
    assert not (home / "store.db").exists()


def test_init_accepts_valid_slug_and_config_loads(tmp_path):
    home = tmp_path / "cardinal-home"
    init = run_init(home, "owner/name")
    assert init.returncode == 0, (init.returncode, init.stdout, init.stderr)
    assert (home / "cardinal.toml").is_file()
    assert load(Home(home)).repos[0].slug == "owner/name"
