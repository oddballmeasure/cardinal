"""Offline `scout` scenario: `cardinal scout --once` surveys the ledger fixture and files reviewed,
code-checked proposals, which the daemon leaves alone until a person approves them.

Pass 1 (propose, cap 2): a real defect is filed with its evidence; a fabricated quote, a duplicate
of an open issue and a reviewer refusal are dropped; the overflow is held. Pass 2 rotates to the
area not yet surveyed and files the held product decision for a person.
"""

import json
import os
import sys
from pathlib import Path

from cardinal_harness import fake_gh, scout_replay
from cardinal_harness.offline import SLUG, expect, world
from cardinal_harness.product import Product, json_file, product_env, write_config

ROLES = ("orchestrator", "profiler", "coder", "verifier", "pr_manager", "deployer", "monitor", "scout", "scout_reviewer")
SCOUT = {"autonomy": "propose", "categories": ["bug", "feature"], "areas_per_pass": 2, "cooldown_days": 7,
         "max_proposals_per_pass": 2, "max_files": 3, "repro": False, "auto_min_decided": 3,
         "auto_min_approval": 0.8, "auto_min_done": 0.7}


def write(home: Path, bare: Path, scout: dict) -> None:
    write_config(home, models={role: "replay:scripted" for role in ROLES}, slug=SLUG, remote_url=str(bare),
                 test_command=[sys.executable, "-m", "pytest", "-q", "tests"], required_checks=[],
                 paths_off_limits=[".github/"], deploy=None, sections={"scout": scout})


def configure(temp: Path, bare: Path, state_path: Path, artifact: Path) -> Product:
    write(temp / "home", bare, SCOUT)
    fake_bin = fake_gh.install(temp / "bin")
    env = product_env({"PATH": f"{fake_bin.parent}{os.pathsep}{os.environ['PATH']}",
                       "CARDINAL_FAKE_GH_STATE": str(state_path),
                       "CARDINAL_MODEL_PROVIDER": "cardinal_harness.replay_provider:provider"})
    return Product(temp / "home", env, artifact)


def by_title(result) -> dict:
    return {item["title"]: item for item in result.get("proposals", [])} if isinstance(result, dict) else {}


def filed(state: dict, title: str) -> dict:
    return next((issue for issue in state["issues"].values() if issue["title"] == title), {})


def scenario(temp: Path, artifact: Path) -> dict:
    bare, state_path, _ = world(temp, ready=[])
    product = configure(temp, bare, state_path, artifact)
    checks: dict = {}

    code, first = product("scout", "--once", "--repo", SLUG)
    seen = by_title(first)
    state = json.loads(state_path.read_text())
    real = filed(state, scout_replay.MISSING_INPUT.title)
    expect(checks, "pass 1 surveys the first two areas", code == 0 and isinstance(first, dict)
           and first.get("areas") == ["input loading", "merchant filter"], first)
    expect(checks, "the real defect is filed as proposed with its checked evidence",
           real.get("labels") == ["cardinal:proposed"] and "`ledger/records.py:8`" in real.get("body", "")
           and "return json.loads(path.read_text())" in real.get("body", "") and "Cardinal-Scout: " in real.get("body", ""),
           real)
    fabricated = seen.get(scout_replay.FABRICATED.title, {})
    expect(checks, "a fabricated quote is dropped by code before review",
           fabricated.get("action") == "dropped" and "does not contain the quoted text" in fabricated.get("reason", "")
           and "verdict" not in fabricated, fabricated)
    duplicate = seen.get(scout_replay.DUPLICATE.title, {})
    expect(checks, "a duplicate of open issue #14 is suppressed",
           duplicate.get("action") == "dropped" and "#14" in duplicate.get("reason", ""), duplicate)
    wrong = seen.get(scout_replay.CASE.title, {})
    expect(checks, "a proposal the reviewer finds wrong is dropped",
           wrong.get("action") == "dropped" and wrong.get("verdict") == "wrong", wrong)
    proposed = [issue for issue in state["issues"].values() if "cardinal:proposed" in issue["labels"]]
    held = seen.get(scout_replay.ACCENTS.title, {})
    expect(checks, "the cap files two and holds the overflow unreviewed",
           len(proposed) == 2 and held.get("action") == "held" and "verdict" not in held, first)

    code, drained = product("daemon", "--once", "--repo", SLUG)
    after = json.loads(state_path.read_text())
    expect(checks, "the daemon never picks up proposed issues", code == 0 and drained == []
           and all(after["issues"][str(issue["number"])]["labels"] == ["cardinal:proposed"] for issue in proposed), drained)

    write(product.home, bare, {**SCOUT, "max_proposals_per_pass": 5})
    code, second = product("scout", "--once", "--repo", SLUG)
    state = json.loads(state_path.read_text())
    expect(checks, "pass 2 rotates to the area not yet surveyed", code == 0 and isinstance(second, dict)
           and second.get("areas") == ["CLI entry point"], second)
    decision = filed(state, scout_replay.ACCENTS.title)
    expect(checks, "the held product decision is filed for a person, not as work",
           decision.get("labels") == ["cardinal:needs-human"] and "product decision" in decision.get("body", ""), decision)

    product.keep_store()
    json_file(artifact / "github_state.json", json.loads(state_path.read_text()))
    return {"checks": checks, "invocations": product.invocations}
