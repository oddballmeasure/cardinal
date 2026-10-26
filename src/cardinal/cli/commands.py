"""Command implementations. Each returns a JSON-able payload, or (payload, exit code)."""

import json
import logging
import os
import shutil
import sys
from contextlib import closing

from cardinal import app
from cardinal.agents.provider import SUBSCRIPTION
from cardinal.config.load import TEMPLATE, load
from cardinal.daemon import loop
from cardinal.github import issues
from cardinal.github.gh import GhError, gh, gh_json
from cardinal.home import SLUG, Home
from cardinal.ingest import server
from cardinal.logs import setup
from cardinal.logs.record import LEVELS, LogRecord
from cardinal.monitor import monitor
from cardinal.repo.git import GitError, git
from cardinal.scout import once as scout
from cardinal.store import logs as stored_logs
from cardinal.store.db import connect

log = logging.getLogger(__name__)
EXIT = {"done": 0, "failed": 1, "awaiting_human": 3, "rejected": 4}
PROVIDER_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def dispatch(args):
    home = Home.resolve(args.home)
    if args.command == "init":
        return init(home, args.repo)
    if args.command == "logs":
        if args.logs_command == "schema":
            return LogRecord.model_json_schema()
        return query_logs(home, args)
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
    if args.command == "daemon":
        repos = [config.repo(args.repo)] if args.repo else config.repos
        if args.once:
            results = [item for repo in repos for item in loop.drain(home, config, repo, db, args.verbose)]
            failed = any(item["status"] == "failed" for item in results)
            return print_and(results, 1 if failed else 0)
        loop.serve(home, config, repos, db, args.interval, args.verbose)
    if args.command == "repos":
        if args.repo:
            return check(home, config, config.repo(args.repo))
        results = [check_result(home, config, repo) for repo in config.repos]
        return print_and(results, 1 if any(not result["ok"] for result in results) else 0)
    repo = config.repo(args.repo)
    if args.command == "run":
        return finish(app.start(home, config, repo, db, args.issue, daemon=args.daemon_worker))
    if args.command == "resume":
        action = "approve" if args.approve else "reject"
        return finish(app.resume(home, config, repo, db, args.issue, action, args.note))
    if args.command == "scout":
        if args.action == "status":
            return scout.status(config, db, repo)
        if args.once:
            result = scout.once(home, config, db, repo)
            return print_and(result, 1 if result.get("failure") else 0)
        if config.scout is None:  # check before serve mode sleeps, as --once and status do
            raise ValueError("No [scout] section in cardinal.toml")
        at = config.scout.at
        if at is not None and args.interval is not None:
            raise ValueError(f"[scout] at = {at!r} schedules a daily pass; --interval cannot be combined with at")
        interval = scout.DEFAULT_INTERVAL if args.interval is None else args.interval
        scout.serve(home, config, db, repo, interval, at)
    raise ValueError(f"Unknown command {args.command}")


def query_logs(home: Home, args) -> int:
    if args.level is not None and args.level not in LEVELS:
        raise ValueError(f"Unknown log level {args.level!r}; expected one of {', '.join(LEVELS)}")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be non-negative")
    with closing(connect(home.store)) as db:
        for row in stored_logs.query(db, args.level, args.repo, args.fingerprint, args.after_seq, args.limit):
            print(json.dumps({"seq": row["seq"], "record": json.loads(row["record"])}))
    return 0


def print_and(payload, code: int) -> int:
    print(json.dumps(payload, indent=2, default=str))
    return code


def finish(result: dict) -> int:
    return print_and(result, EXIT.get(result["status"], 1))


def init(home: Home, slug: str) -> dict:
    if not SLUG.fullmatch(slug):
        raise ValueError(f"Repository must be owner/name, got {slug!r}")
    home.root.mkdir(parents=True, exist_ok=True)
    if home.config.exists():
        raise ValueError(f"{home.config} already exists")
    home.config.write_text(TEMPLATE.format(slug=slug))
    connect(home.store).close()
    return {"config": str(home.config), "store": str(home.store)}


def check(home: Home, config, repo) -> int:
    result = check_result(home, config, repo)
    return print_and(result, 0 if result["ok"] else 1)


def check_result(home: Home, config, repo) -> dict:
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
    if repo.focused_test_command is not None:
        probe("focused test command", lambda: _executable(repo.focused_test_command[0]))
    for role, spec in config.models.model_dump(exclude_none=True).items():
        probe(f"model {role}", lambda spec=spec: _model_key(spec, config.ceiling))
    failed = any(value.startswith("FAILED") for value in results.values())
    return {"repo": repo.slug, "checks": results, "ok": not failed}


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


def _model_key(spec: str, ceiling: float | None = None) -> str:
    if os.environ.get("CARDINAL_MODEL_PROVIDER"):
        return "model provider hook set"
    if spec.startswith(SUBSCRIPTION):
        from cardinal.agents import claude_code  # the SDK is slow to import; only this probe needs it
        return claude_code.probe(spec, ceiling)
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
            row["agent_calls"] = [
                {key: value for key, value in dict(call).items()
                 if value is not None or key not in ("model", "window", "utilization")} | {"transcript": None}
                for call in db.execute(
                    "SELECT stage, ticket_id, seconds, input_tokens, output_tokens, tool_calls, outcome, transcript,"
                    " model, \"window\", utilization FROM agent_calls WHERE run_id = ? ORDER BY id", (row["run_id"],))]
        return rows
    if not rows:
        print("No runs recorded.", file=sys.stderr)
    return [{key: row[key] for key in ("repo", "issue", "status", "failure_kind", "pr_url", "merge_sha", "started_at", "ended_at")}
            for row in rows]
