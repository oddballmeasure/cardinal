"""Command implementations. Each returns a JSON-able payload, or (payload, exit code)."""

import json
import logging
import os
import shutil
import sys

from cardinal import app
from cardinal.config.load import TEMPLATE, load
from cardinal.daemon import loop
from cardinal.github import issues
from cardinal.github.gh import GhError, gh, gh_json
from cardinal.home import Home
from cardinal.ingest import server
from cardinal.logs import setup
from cardinal.logs.record import LogRecord
from cardinal.monitor import monitor
from cardinal.repo.git import GitError, git
from cardinal.store.db import connect

log = logging.getLogger(__name__)
EXIT = {"done": 0, "failed": 1, "awaiting_human": 3, "rejected": 4}
PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def dispatch(args):
    home = Home.resolve(args.home)
    if args.command == "init":
        return init(home, args.repo)
    if args.command == "logs":
        return LogRecord.model_json_schema()
    config = load(home)
    db = connect(home.store)
    sink = setup.install(home, config, db)
    try:
        return route(args, home, config, db, sink)
    except (ValueError, FileNotFoundError):
        raise
    except Exception:
        log.critical("cardinal %s crashed", args.command, exc_info=True, extra={"event": "command_crashed"})
        raise


def route(args, home: Home, config, db, sink):
    if args.command == "status":
        return status(db, args.repo and config.repo(args.repo).slug, args.issue, args.json)
    if args.command == "ingest":
        return server.serve(config, sink)
    if args.command == "monitor":
        if args.once:
            return monitor.once(home, config, db)
        monitor.serve(home, config, db, args.interval)
    repo = config.repo(args.repo)
    if args.command == "repos":
        return check(home, config, repo)
    if args.command == "run":
        return finish(app.start(home, config, repo, db, args.issue))
    if args.command == "resume":
        action = "approve" if args.approve else "reject"
        return finish(app.resume(home, config, repo, db, args.issue, action, args.note))
    if args.command == "daemon":
        if args.once:
            results = loop.drain(home, config, repo, db)
            failed = any(item["status"] == "failed" for item in results)
            return print_and(results, 1 if failed else 0)
        loop.serve(home, config, repo, db, args.interval)
    raise ValueError(f"Unknown command {args.command}")


def print_and(payload, code: int) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return code


def finish(result: dict) -> int:
    return print_and(result, EXIT.get(result["status"], 1))


def init(home: Home, slug: str) -> dict:
    home.root.mkdir(parents=True, exist_ok=True)
    if home.config.exists():
        raise ValueError(f"{home.config} already exists")
    home.config.write_text(TEMPLATE.format(slug=slug))
    connect(home.store).close()
    return {"config": str(home.config), "store": str(home.store)}


def check(home: Home, config, repo) -> int:
    results: dict[str, str] = {}

    def probe(name: str, action) -> None:
        try:
            results[name] = f"ok {action() or ''}".strip()
        except (GhError, GitError, OSError, ValueError, KeyError) as exc:
            results[name] = f"FAILED {exc}"
            log.error("repos check probe %s failed", name, exc_info=exc,
                      extra={"event": "probe_failed", "data": {"probe": name, "repo": repo.slug}})

    probe("gh auth", lambda: gh("auth", "status") and None)
    probe("repository", lambda: gh_json("repo", "view", repo.slug, "--json", "nameWithOwner")["nameWithOwner"])
    probe("base branch", lambda: _base(home, repo))
    probe("labels", lambda: issues.ensure_labels(repo.slug, repo.labels))
    probe("test command", lambda: _executable(repo.test_command[0]))
    for role, spec in config.models.model_dump().items():
        probe(f"model {role}", lambda spec=spec: _model_key(spec))
    failed = any(value.startswith("FAILED") for value in results.values())
    return print_and({"repo": repo.slug, "checks": results, "ok": not failed}, 1 if failed else 0)


def _base(home: Home, repo) -> str:
    out = git(home.root, "ls-remote", repo.url, f"refs/heads/{repo.base_branch}")
    if not out.strip():
        raise ValueError(f"{repo.base_branch} not found on {repo.url}")
    return out.split()[0][:12]


def _executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise ValueError(f"{name} is not on PATH")
    return found


def _model_key(spec: str) -> str:
    if os.environ.get("CARDINAL_MODEL_PROVIDER"):
        return "model provider hook set"
    provider = spec.partition(":")[0]
    key = PROVIDER_KEYS.get(provider)
    if key is None:
        raise ValueError(f"Unknown model provider {provider!r}")
    if not os.environ.get(key):
        raise ValueError(f"{key} is not set in the environment")
    return key


def status(db, repo: str | None, issue: int | None, detail: bool):
    query, params = "SELECT * FROM runs", []
    clauses = []
    if repo:
        clauses.append("repo = ?")
        params.append(repo)
    if issue is not None:
        clauses.append("issue = ?")
        params.append(issue)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    rows = [dict(row) for row in db.execute(query + " ORDER BY started_at, rowid", params)]
    if detail:
        for row in rows:
            row["events"] = [
                {"at": event["at"], "stage": event["stage"], "kind": event["kind"], "data": json.loads(event["data"])}
                for event in db.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id", (row["run_id"],))
            ]
            row["agent_calls"] = [dict(call) | {"transcript": None} for call in db.execute(
                "SELECT stage, ticket_id, seconds, input_tokens, output_tokens, tool_calls, outcome, transcript"
                " FROM agent_calls WHERE run_id = ? ORDER BY id", (row["run_id"],))]
        return rows
    if not rows:
        print("No runs recorded.", file=sys.stderr)
    return [{key: row[key] for key in ("issue", "status", "failure_kind", "pr_url", "merge_sha", "started_at", "ended_at")}
            for row in rows]
