"""`live-scout`: one real `cardinal scout --once` pass against the live test repository, graded on
whether it finds defects planted for it.

Under the live lock: push `scout-seed-<run>` (the suite's pinned baseline plus one commit built from
tests/fixtures/scout_seed) and point a temporary Cardinal home at it in propose mode. Main is never
touched. After the pass, grade from GitHub and the temporary store. A filed issue matches a finding
in tests/fixtures/scout_live.json when its Evidence section cites one of the finding's paths and
every keyword group has a case-insensitive regex hit in its title or body. Planted findings are hard
checks; the baseline's own zero-notes bug is recorded only. Cleanup always runs: the pass's issues
are closed with a comment and the seed branch is deleted, unless --keep-issues leaves both for a
person to read and `live-scout --cleanup <run>` removes them later.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cardinal_harness.github_setup import gh, load_model_key, use_github_cli_git_credentials
from cardinal_harness.live import ROLES as DAEMON_ROLES
from cardinal_harness.live import Target, exclusive
from cardinal_harness.product import ROOT, Product, json_file, product_env, write_config

FIXTURE = ROOT / "tests" / "fixtures" / "scout_live.json"
ROLES = (*DAEMON_ROLES, "scout", "scout_reviewer")
PREFIX = "scout-seed-"
MANIFEST = "live_scout.json"
MARKER = "Cardinal-Scout: "  # the footer render.body writes on every scout issue
PROPOSED, READY, NEEDS_HUMAN = "cardinal:proposed", "cardinal:ready", "cardinal:needs-human"
CLEANUP = "uv run --locked --extra openai --extra e2e python -m cardinal_harness live-scout --cleanup {run_id}"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True, timeout=300).stdout.strip()


def commit_seed(clone: Path, baseline: str, spec: dict) -> str:
    """One commit on the baseline that writes the seed fixture's files; returns its SHA."""
    git(clone, "switch", "-q", "--detach", baseline)
    shutil.copytree(ROOT / spec["seed"], clone, dirs_exist_ok=True)
    git(clone, "add", "-A")
    git(clone, "-c", "user.name=Cardinal harness", "-c", "user.email=harness@example.invalid",
        "commit", "-qm", spec["seed_message"])
    return git(clone, "rev-parse", "HEAD")


def branch_exists(url: str, branch: str) -> bool:
    return bool(subprocess.run(["git", "ls-remote", url, f"refs/heads/{branch}"], text=True, capture_output=True,
                               check=True, timeout=60).stdout.strip())


def scout_issues(repository: str, since: str) -> list[dict]:
    """Scout-filed issues created since `since`, read from GitHub (the listing, not search, which lags)."""
    items = json.loads(gh("issue", "list", "-R", repository, "--state", "all", "--limit", "100",
                          "--json", "number,title,body,labels,state,url,createdAt"))
    return sorted((item for item in items if item["createdAt"] >= since and MARKER in item["body"]),
                  key=lambda item: item["number"])


def read_store(path: Path) -> dict:
    if not path.is_file():
        return {"passes": [], "proposals": [], "usage": {}}
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        passes = [dict(row) for row in db.execute("SELECT * FROM scout_passes")]
        proposals = [dict(row) for row in db.execute(
            "SELECT category, title, status, reason, verdict, repro_ok, filed_mode, issue FROM scout_proposals ORDER BY id")]
        usage = {row["stage"]: dict(row) for row in db.execute(
            "SELECT stage, count(*) AS calls, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens,"
            " round(sum(seconds)) AS seconds FROM agent_calls GROUP BY stage")}
    return {"passes": passes, "proposals": proposals, "usage": usage}


def evidence_paths(body: str) -> set[str]:
    section = body.split("## Evidence", 1)[1].split("## Expected", 1)[0] if "## Evidence" in body else ""
    return set(re.findall(r"^`([^`]+?):\d+", section, re.MULTILINE))


def matches(issue: dict, finding: dict) -> bool:
    text = f"{issue['title']}\n{issue['body']}"
    return bool(evidence_paths(issue["body"]) & set(finding["paths"])) and all(
        any(re.search(pattern, text, re.IGNORECASE) for pattern in group) for group in finding["keywords"])


def grade(report: dict, spec: dict, code: int, result: object, store: dict, filed: list[dict]) -> None:
    checks = report["checks"]

    def expect(name: str, ok: bool, detail: object = None) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    passes, proposals = store["passes"], store["proposals"]
    expect("the scout pass finished without failure",
           code == 0 and isinstance(result, dict) and not result.get("failure") and len(passes) == 1
           and passes[0]["failure_kind"] is None and passes[0]["ended_at"] is not None,
           {"exit": code, "failure": result.get("failure") if isinstance(result, dict) else result, "passes": passes})
    from_store = sorted(row["issue"] for row in proposals if row["status"] == "filed")
    expect("the store and GitHub agree on the filed issues", from_store == [item["number"] for item in filed],
           {"store": from_store, "github": [item["number"] for item in filed]})
    labels = {item["number"]: sorted(label["name"] for label in item["labels"]) for item in filed}
    # A product decision is filed for a person as needs-human, never as proposed; neither is work.
    expect("every filed issue is cardinal:proposed (or needs-human) and none is cardinal:ready",
           all(READY not in names and (PROPOSED in names or NEEDS_HUMAN in names) for names in labels.values()), labels)
    baseline = passes[0]["baseline_passed"] if passes else None
    expect("the seeded base's suite was green wherever the scout ran it (repro precondition)", baseline != 0, baseline)

    hits = {finding["key"]: [item["number"] for item in filed if matches(item, finding)] for finding in spec["findings"]}
    report["natural"] = {}
    for finding in spec["findings"]:
        if finding["planted"]:
            expect(f"planted {finding['key']} is filed", hits[finding["key"]], hits[finding["key"]])
        else:
            report["natural"][finding["key"]] = {"matched": bool(hits[finding["key"]]), "issues": hits[finding["key"]]}
    matched = {number for numbers in hits.values() for number in numbers}
    report["precision"] = {
        "filed": len(filed), "matched": len(matched),
        "needs_human": sum(NEEDS_HUMAN in names for names in labels.values()),
        "unmatched": [f"#{item['number']} {item['title']}" for item in filed if item["number"] not in matched],
        "dropped": [{key: row[key] for key in ("title", "verdict", "reason")} for row in proposals if row["status"] == "dropped"],
        "held": [{key: row[key] for key in ("title", "reason")} for row in proposals if row["status"] == "held"],
        "repro": [{key: row[key] for key in ("title", "status", "repro_ok", "reason")}
                  for row in proposals if row["repro_ok"] is not None],
    }
    report["areas"] = result.get("areas") if isinstance(result, dict) else None


def clean(repository: str, url: str, branch: str, numbers: list[int], run_id: str) -> dict:
    """Close the pass's issues with a comment and delete the seed branch, then check both from GitHub."""
    if not branch.startswith(PREFIX):
        raise ValueError(f"Refusing to delete {branch}: not a seed branch")
    errors = []
    for number in numbers:
        try:
            if json.loads(gh("issue", "view", str(number), "-R", repository, "--json", "state"))["state"] == "OPEN":
                gh("issue", "close", str(number), "-R", repository, "--comment", f"Closed by the live scout test (run {run_id})")
        except subprocess.CalledProcessError as exc:
            errors.append(f"#{number}: {exc.stderr.strip()[:300]}")
    if branch_exists(url, branch):
        gh("api", "-X", "DELETE", f"repos/{repository}/git/refs/heads/{branch}")
    still_open = [number for number in numbers
                  if json.loads(gh("issue", "view", str(number), "-R", repository, "--json", "state"))["state"] == "OPEN"]
    branch_left = branch_exists(url, branch)
    return {"closed": [number for number in numbers if number not in still_open], "open": still_open,
            "branch_deleted": not branch_left, "errors": errors, "clean": not still_open and not branch_left}


def run(model: str, artifact: Path, keep: bool) -> dict:
    return exclusive(run_locked, model, artifact, keep)


def run_locked(model: str, artifact: Path, keep: bool) -> dict:
    spec = json.loads(FIXTURE.read_text())
    suite = json.loads((ROOT / spec["suite"]).read_text())
    target = Target.of(suite)
    repository, url = suite["repository"], f"https://github.com/{suite['repository']}"
    run_id = artifact.name[:12]
    branch = PREFIX + run_id
    manifest = {"run_id": run_id, "repository": repository, "branch": branch, "baseline_sha": suite["baseline_sha"],
                "seed_sha": None, "issues": []}
    report: dict = {"run_id": run_id, "model": model, "branch": branch, "checks": {}, "passed": False}
    started = time.monotonic()
    # GitHub's clock, not ours, stamps createdAt; a minute's slack keeps a fast pass's first issue in.
    since = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    product: Product | None = None
    seeding = False
    with tempfile.TemporaryDirectory(prefix="cardinal-live-scout-") as name:
        temp = Path(name)
        try:
            load_model_key()
            use_github_cli_git_credentials()
            clone = temp / "seed"
            subprocess.run(["git", "clone", "-q", url, str(clone)], check=True, capture_output=True, timeout=300)
            manifest["seed_sha"] = commit_seed(clone, suite["baseline_sha"], spec)
            seeding = True  # from here a push may have landed, so cleanup must look for the branch
            git(clone, "push", "-q", "origin", f"{manifest['seed_sha']}:refs/heads/{branch}")
            json_file(artifact / MANIFEST, manifest)
            home = temp / "home"
            write_config(home, models={role: model for role in ROLES}, slug=repository, remote_url=url,
                         test_command=target.test_command, required_checks=[target.check],
                         paths_off_limits=target.paths_off_limits, deploy=None, sections={"scout": spec["scout"]},
                         base_branch=branch)
            product = Product(home, product_env({"OPENAI_API_KEY": os.environ["OPENAI_API_KEY"]}), artifact)
            code, report["repos_check"] = product("repos", "check")
            if code != 0:
                raise ValueError(f"cardinal repos check failed: {report['repos_check']}")
            code, result = product("scout", "--repo", repository, "--once")
            report["scout_seconds"] = product.invocations[-1]["seconds"]
            json_file(artifact / "scout_pass.json", result)
            store = read_store(home / "store.db")
            report["usage"] = store["usage"]
            grade(report, spec, code, result, store, scout_issues(repository, since))
        except Exception as exc:  # noqa: BLE001 - recorded, then cleanup still runs
            report["error"] = f"{type(exc).__name__}: {exc}"
            (artifact / "error.txt").write_text(traceback.format_exc())
        finally:
            if product is not None:
                product.keep_store()
                report["invocations"] = product.invocations
            if seeding:
                report["cleanup"] = finish(report, manifest, product, repository, url, since, keep, artifact)
    report["wall_seconds"] = round(time.monotonic() - started)
    cleanup = report.get("cleanup") or {}
    report["passed"] = bool(report["checks"]) and "error" not in report and all(
        item["ok"] for item in report["checks"].values()) and bool(cleanup.get("clean") or cleanup.get("kept"))
    return report


def finish(report: dict, manifest: dict, product: Product | None, repository: str, url: str, since: str, keep: bool,
           artifact: Path) -> dict:
    """Every issue the pass filed, from the store and from GitHub, then close them or keep them."""
    try:
        recorded = read_store(product.home / "store.db")["proposals"] if product else []
        numbers = {row["issue"] for row in recorded if row["status"] == "filed"}
        found = scout_issues(repository, since)
        manifest["issues"] = sorted(numbers | {item["number"] for item in found})
        json_file(artifact / MANIFEST, manifest)
        if keep:
            urls = [f"https://github.com/{repository}/issues/{number}" for number in manifest["issues"]]
            command = CLEANUP.format(run_id=manifest["run_id"])
            print("\n".join(["Kept the scout's issues and the seed branch " + manifest["branch"], *urls,
                             f"Clean up later with: {command}"]), file=sys.stderr)
            return {"kept": True, "issues": urls, "branch": manifest["branch"], "command": command}
        return clean(repository, url, manifest["branch"], manifest["issues"], manifest["run_id"])
    except Exception as exc:  # noqa: BLE001
        (artifact / "cleanup_error.txt").write_text(traceback.format_exc())
        return {"clean": False, "error": f"{type(exc).__name__}: {exc}",
                "command": CLEANUP.format(run_id=manifest["run_id"])}


def cleanup_later(run_id: str) -> dict:
    """Close a kept run's issues and delete its seed branch, from the manifest that run wrote."""
    found = [path for path in (ROOT / "artifacts" / "e2e").glob(f"*/{MANIFEST}")
             if json.loads(path.read_text())["run_id"] == run_id]
    if len(found) != 1:
        return {"passed": False, "error": f"Expected one {MANIFEST} for run {run_id}; found {len(found)}"}
    manifest = json.loads(found[0].read_text())
    use_github_cli_git_credentials()
    url = f"https://github.com/{manifest['repository']}"
    result = exclusive(clean, manifest["repository"], url, manifest["branch"], manifest["issues"], run_id)
    return {"run_id": run_id, "manifest": str(found[0]), "cleanup": result, "passed": bool(result.get("clean"))}
