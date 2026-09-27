"""`cardinal monitor`: turn repeated errors into triaged issues on the repository that produced them.

Rules decide what is filed; a model only writes the words. The cursor never moves past a group
that was due but not filed (capped, failed, or unroutable), so it is reconsidered next pass.
"""

import json
import logging
import sqlite3
import time
import uuid

from cardinal import app
from cardinal.agents.runner import AgentCall, run_agent
from cardinal.config.models import Config
from cardinal.contracts.monitor import IssueDraft
from cardinal.github import issues
from cardinal.home import Home
from cardinal.logs.context import bind
from cardinal.monitor.scan import Group, due, fill, new_groups
from cardinal.store import findings
from cardinal.store.recorder import Recorder

log = logging.getLogger(__name__)


def evidence_block(group: Group, previous: int | None) -> str:
    error = group.sample.get("error") or {}
    lines = ["", "---", f"Cardinal-Fingerprint: {group.fingerprint}",
             f"Occurrences: {group.count} between {group.first_seen} and {group.last_seen}",
             f"Component: {group.sample.get('source', {}).get('component')}"]
    if group.run_ids:
        lines.append("Runs: " + ", ".join(group.run_ids))
    if previous:
        lines.append(f"Recurs after #{previous}")
    if error.get("traceback"):
        lines += ["", "```", error["traceback"][-6000:], "```"]
    return "\n".join(lines)


def draft(home: Home, config: Config, db: sqlite3.Connection, group: Group) -> IssueDraft:
    call_id = f"monitor-{uuid.uuid4().hex[:8]}"
    context_dir = home.run_dir(call_id) / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "finding.json").write_text(json.dumps(group.evidence(), indent=2, default=str))
    call = AgentCall(stage="monitor", model_spec=config.models.monitor, context_dir=context_dir,
                     context={"repo": group.repo, "fingerprint": group.fingerprint, "run_id": call_id},
                     prompt="Use the monitor skill. Read /context/finding.json and return one IssueDraft.",
                     schema=IssueDraft)
    limits = config.limits
    return run_agent(call, Recorder(db, call_id), limits.recursion_limit, limits.model_timeout_seconds)


def file(home: Home, config: Config, db: sqlite3.Connection, group: Group) -> dict:
    repo = config.repo(group.repo)  # ValueError when the source repository is not configured
    existing = findings.get(db, group.fingerprint)
    previous = None
    if existing is not None:
        _, _, state = issues.view(existing["repo"], existing["issue"])
        if state == "OPEN":
            findings.seen(db, group.fingerprint, group.count, group.last_seen)
            return {"fingerprint": group.fingerprint, "issue": existing["issue"], "action": "already_open"}
        previous = existing["issue"]
    text = draft(home, config, db, group)
    number = issues.create(repo.slug, text.title, text.body + "\n" + evidence_block(group, previous))
    findings.record(db, group.fingerprint, repo.slug, number, group.count, group.first_seen, group.last_seen)
    log.info("filed #%s on %s for %s", number, repo.slug, group.fingerprint,
             extra={"event": "finding_filed", "data": {"issue": number, "repo": repo.slug}})
    triaged = app.triage(home, config, repo, db, number)
    return {"fingerprint": group.fingerprint, "issue": number, "repo": repo.slug, "action": "filed",
            "label": triaged["label"], "recurs_after": previous}


def once(home: Home, config: Config, db: sqlite3.Connection) -> list[dict]:
    if config.monitor is None:
        raise ValueError("No [monitor] section in cardinal.toml")
    settings = config.monitor
    with bind(stage="monitor"):
        start = findings.cursor(db)
        groups, top = new_groups(db, start)
        results, held, filed = [], [], 0
        for group in (fill(db, group, settings) for group in groups):
            if not due(group, settings):
                continue  # below threshold: a later record of this fingerprint brings it back
            if filed >= settings.max_issues_per_pass:
                held.append(group.first_seq)
                results.append({"fingerprint": group.fingerprint, "action": "held_by_cap"})
                continue
            try:
                result = file(home, config, db, group)
            except Exception as exc:  # noqa: BLE001 - one finding failing must not stop the rest
                log.error("could not file %s for %s", group.fingerprint, group.repo, exc_info=exc,
                          extra={"event": "finding_failed", "data": {"fingerprint": group.fingerprint}})
                held.append(group.first_seq)
                results.append({"fingerprint": group.fingerprint, "action": "failed", "error": str(exc)[:500]})
                continue
            filed += result["action"] == "filed"
            results.append(result)
        findings.advance(db, min(held) - 1 if held else top)
    return results


def serve(home: Home, config: Config, db: sqlite3.Connection, interval: int) -> None:
    while True:
        try:
            once(home, config, db)
        except Exception:  # noqa: BLE001 - a failed pass is logged and retried; the monitor keeps watching
            log.exception("monitor pass failed", extra={"event": "monitor_pass_failed"})
        time.sleep(interval)
