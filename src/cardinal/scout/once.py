"""`cardinal scout`: find small, verifiable work in a repository and propose it as issues.

Rules decide what is filed; models find and argue. A planner splits the repository into areas, a
survey reads one area at a time, and an independent reviewer re-derives each proposal from the code.
Code checks every quote and path, dedupes against the store and open issues, caps each pass and
holds the overflow for the next. Filed issues carry `cardinal:proposed`, which the daemon ignores
until a person swaps it for `cardinal:ready`, and a type label for their category (`cardinal:bug`).
"""

import itertools
import json
import logging
import sqlite3
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta

from cardinal.agents.backends import under
from cardinal.config.models import Config, Repo
from cardinal.contracts.scout import REPRO_CATEGORIES, Area, Proposal
from cardinal.github import issues
from cardinal.github.gh import GhError
from cardinal.graph.context import ScoutRun
from cardinal.graph.failures import FailureKind, StageFailure
from cardinal.home import Home, safe
from cardinal.locks import lock
from cardinal.logs.context import bind
from cardinal.repo.git import GitError, detached_worktree, ensure_clone, fetch_base, remove_worktree
from cardinal.roles import profiler, scout, scout_planner, scout_repro, scout_reviewer
from cardinal.roles.tests import run_repo_tests
from cardinal.scout import gate, outcomes, render
from cardinal.scout.checks import docs_only, evidence_problem, fingerprint, proposal_problem, similar
from cardinal.store import scout as store
from cardinal.store.db import now
from cardinal.store.recorder import Recorder

log = logging.getLogger(__name__)
WINDOW_DAYS = outcomes.WINDOW_DAYS
DEFAULT_INTERVAL = 86400


def area_key(area: Area) -> str:
    return ",".join(sorted({path.strip("/") for path in area.paths}))


def choose(areas: list[Area], last: dict[str, str], cooldown_days: float, limit: int) -> list[Area]:
    """Areas not surveyed within the cooldown, never-surveyed first, then the longest ago."""
    cutoff = store.ago(cooldown_days)
    unique = {area_key(area): area for area in reversed(areas)}
    fresh = [area for area in areas if unique.get(area_key(area)) is area and last.get(area_key(area), "") <= cutoff]
    return sorted(fresh, key=lambda area: last.get(area_key(area), ""))[:limit]


def failure_kind(exc: BaseException) -> FailureKind:
    if isinstance(exc, StageFailure):
        return exc.kind
    return {GhError: FailureKind.GITHUB, GitError: FailureKind.GIT}.get(type(exc), FailureKind.CRASH)


class Pass:
    def __init__(self, run: ScoutRun) -> None:
        self.run, self.settings = run, run.config.scout
        self.results: list[dict] = []
        self.seen: set[str] = set()
        self.filed = 0
        self.repros = itertools.count(1)
        self.green: bool | None = None

    @property
    def slug(self) -> str:
        return self.run.repo.slug

    def execute(self) -> dict:
        run, repo, db = self.run, self.run.repo, self.run.db
        decided = outcomes.track(repo, db)
        self.track_record = outcomes.track_record(db, repo.slug)
        issues.ensure_labels(repo.slug, repo.labels)  # lessons/labels-before-first-write.md
        issues.ensure_type_labels(repo.slug, repo.labels, self.settings.categories)
        ensure_clone(run.clone, repo.url, repo.base_branch)
        self.base_sha = fetch_base(run.clone, run.clone, repo.base_branch)
        store.update_pass(db, run.run_id, base_sha=self.base_sha)
        detached_worktree(run.clone, run.worktree, self.base_sha)
        surveyed = []
        try:
            with lock(run.clone, "intake"):  # the daemon's planning writes the same profile row
                profile = profiler.profile(run)
            self.tracked = {path for path in profiler.tracked_files(run.worktree) if not under(path, repo.paths_hidden)}
            self.issues = issues.titles(repo.slug)
            run.write_context("repo_profile.json", profiler.dump(profile))
            run.write_context("issues.json", self.issues)
            run.write_context("rejections.json", store.rejections(db, repo.slug, WINDOW_DAYS))
            run.write_context("rules.json", {"categories": self.settings.categories, "max_files": self.settings.max_files,
                                             "paths_off_limits": repo.paths_off_limits, "test_command": repo.test_command})
            for row in store.held(db, repo.slug):
                self.consider(Proposal.model_validate_json(row["proposal"]), row["id"])
            areas = scout_planner.plan(run, profile, self.tracked)
            for area in choose(areas, store.area_times(db, repo.slug), self.settings.cooldown_days,
                               self.settings.areas_per_pass):
                try:
                    findings = scout.survey(run, area)
                except StageFailure as exc:  # one area failing must not lose the others
                    log.log(exc.kind.level, "survey of %s failed", area.name, exc_info=exc,
                            extra={"event": "scout_survey_failed", "failure_kind": exc.kind.value})
                    continue
                store.surveyed(db, repo.slug, area_key(area))
                surveyed.append(area.name)
                for proposal in findings.proposals:
                    self.consider(proposal, None)
        finally:
            remove_worktree(run.clone, run.worktree)
        counts = dict(Counter(item["action"] for item in self.results))
        store.update_pass(db, run.run_id, counts=json.dumps(counts), ended_at=now())
        return {"pass_id": run.run_id, "repo": repo.slug, "base_sha": self.base_sha, "autonomy": self.settings.autonomy,
                "outcomes": decided, "areas": surveyed, "proposals": self.results, "counts": counts}

    def base_green(self) -> bool:
        """The test command on the base commit, run once per base SHA: a red base proves no repro."""
        if self.green is None:
            self.green = store.baseline(self.run.db, self.slug, self.base_sha)
        if self.green is None:
            self.green = run_repo_tests(self.run.worktree, self.run.repo.test_command,
                                        self.run.config.limits.test_timeout_seconds).passed
            store.update_pass(self.run.db, self.run.run_id, baseline_passed=int(self.green))
        return self.green

    def record(self, row_id: int | None, proposal: Proposal, status: str, **fields: object) -> None:
        store.record(self.run.db, row_id, repo=self.slug, pass_id=self.run.run_id, fingerprint=fingerprint(proposal),
                     proposal=proposal.model_dump(), status=status, **fields)
        self.results.append({"title": proposal.title, "category": proposal.category, "action": status,
                             **{key: value for key, value in fields.items() if value is not None}})

    def duplicate(self, proposal: Proposal) -> str | None:
        prior = store.latest_filed(self.run.db, self.slug, fingerprint(proposal))
        if prior is not None and (prior["outcome"] in (None, "approved", "error") or
                                  prior["outcome"] == "rejected" and prior["decided_at"] >= store.ago(WINDOW_DAYS)):
            return f"already filed as #{prior['issue']} ({prior['outcome'] or 'undecided'})"
        for item in self.issues:
            if item["state"] == "OPEN" and similar(item["title"], proposal.title):
                return f"duplicates open issue #{item['number']}: {item['title']}"
        return None

    def consider(self, proposal: Proposal, row_id: int | None) -> None:
        key = fingerprint(proposal)
        if key in self.seen:
            return self.record(row_id, proposal, "dropped", reason="proposed twice in this pass")
        self.seen.add(key)
        problem = proposal_problem(self.run.worktree, proposal, self.tracked, self.run.repo, self.settings)
        problem = problem or self.duplicate(proposal)
        if problem:
            return self.record(row_id, proposal, "dropped", reason=problem)
        if self.filed >= self.settings.max_proposals_per_pass:
            return self.record(row_id, proposal, "held", reason=f"this pass already filed {self.filed}")
        try:
            self.weigh(proposal, row_id)
        except Exception as exc:  # noqa: BLE001 - one proposal failing must not stop the rest; it is weighed again next pass
            log.error("could not weigh proposal %r", proposal.title, exc_info=exc, extra={"event": "scout_proposal_failed"})
            self.record(row_id, proposal, "held", reason=f"{type(exc).__name__}: {str(exc)[:300]}")

    def weigh(self, proposal: Proposal, row_id: int | None) -> None:
        run, repo = self.run, self.run.repo
        review = scout_reviewer.review(run, proposal)
        if review.verdict not in ("confirmed", "product_decision"):
            return self.record(row_id, proposal, "dropped", verdict=review.verdict, reason=review.reason)
        if review.category not in (None, proposal.category) and review.category in self.settings.categories:
            log.info("reviewer recategorised %r as %s", proposal.title, review.category,
                     extra={"event": "scout_recategorised", "data": {"from": proposal.category, "to": review.category}})
            proposal = proposal.model_copy(update={"category": review.category})
            if proposal.category == "docs" and not docs_only(proposal):
                return self.record(row_id, proposal, "dropped", verdict=review.verdict,
                                   reason="reviewer called it docs, but the fix changes more than documentation")
        evidence = proposal.evidence
        if review.verdict == "confirmed":
            problem = evidence_problem(run.worktree, review.evidence, self.tracked, repo)
            if problem:
                return self.record(row_id, proposal, "dropped", verdict=review.verdict, reason=f"reviewer {problem}")
            evidence = review.evidence
        repro = None
        if review.verdict == "confirmed" and proposal.category in REPRO_CATEGORIES and self.settings.repro and not docs_only(proposal):
            repro = scout_repro.reproduce(run, proposal, self.base_sha, self.base_green(), next(self.repros))
            run.recorder.event("scout", "repro", {"title": proposal.title, "ok": repro.ok, "reason": repro.reason})
            if repro.refuted:
                return self.record(row_id, proposal, "dropped", verdict=review.verdict, repro_ok=0, reason=repro.reason)
        if review.verdict == "product_decision":  # never work Cardinal may take on by itself
            mode, why = "needs_human", ""
        else:
            mode, why = gate.decide(self.settings, self.track_record, proposal.category, repro and repro.ok)
        label = {"proposed": repo.labels.proposed, "auto": repo.labels.ready, "needs_human": repo.labels.needs_human}[mode]
        test_diff = repro.diff if repro and repro.ok else None
        text = render.body(proposal, evidence, review.reason, mode, why, repo.labels, fingerprint(proposal), test_diff)
        number = issues.create(repo.slug, proposal.title, text, labels=(label, repo.labels.type_label(proposal.category)))
        self.filed += 1
        self.issues.append({"number": number, "title": proposal.title, "state": "OPEN"})
        run.write_context("issues.json", self.issues)  # later reviewers and surveys in this pass read it
        log.info("filed #%s as %s", number, mode, extra={"event": "scout_filed", "data": {"issue": number, "mode": mode}})
        notes = "; ".join(item for item in (why, repro and repro.reason) if item)
        self.record(row_id, proposal, "filed", verdict=review.verdict, filed_mode=mode, issue=number, reason=notes or None,
                    repro_ok=None if repro is None else int(repro.ok))


def once(home: Home, config: Config, db: sqlite3.Connection, repo: Repo) -> dict:
    if config.scout is None:
        raise ValueError("No [scout] section in cardinal.toml")
    pass_id = f"scout-{uuid.uuid4().hex[:8]}"
    store.start_pass(db, pass_id, repo.slug, config.scout.autonomy)
    worktree = home.root / "worktrees" / safe(repo.slug) / pass_id
    run = ScoutRun(home, config, repo, db, Recorder(db, pass_id), pass_id, worktree)
    with bind(run_id=pass_id, stage="scout"):
        try:
            return Pass(run).execute()
        except Exception as exc:  # noqa: BLE001 - a failed pass is recorded with its kind and reported
            kind = failure_kind(exc)
            log.log(kind.level, "scout pass %s failed", pass_id, exc_info=exc,
                    extra={"event": "scout_pass_failed", "failure_kind": kind.value})
            store.update_pass(db, pass_id, failure_kind=kind.value, failure_detail=str(exc)[:2000], ended_at=now())
            return {"pass_id": pass_id, "repo": repo.slug, "failure": {"kind": kind.value, "detail": str(exc)[:500]}}


def next_at(at: str, after: datetime | None = None) -> datetime:
    """The next host-local occurrence of HH:MM strictly after `after`: today if still ahead, else tomorrow."""
    current = (after or datetime.now()).astimezone().replace(tzinfo=None)
    hour, minute = (int(part) for part in at.split(":"))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return target.astimezone()  # naive local time -> aware, following the host's DST rules


def serve(home: Home, config: Config, db: sqlite3.Connection, repo: Repo, interval: int = DEFAULT_INTERVAL,
          at: str | None = None) -> None:
    """Run passes forever: daily at host-local `at` when set, else every `interval` seconds. No failure ends it."""
    if config.scout is None:  # a missing section is a configuration error, not a pass failure
        raise ValueError("No [scout] section in cardinal.toml")
    while True:
        if at is not None:
            when = next_at(at)
            delay = max(0.0, (when - datetime.now().astimezone()).total_seconds())
        else:
            delay = interval
            when = datetime.now().astimezone() + timedelta(seconds=interval)
        log.info("next scout pass at %s", when.isoformat(),
                 extra={"event": "scout_next_pass", "data": {"at": when.isoformat()}})
        time.sleep(delay)
        try:
            once(home, config, db, repo)  # records and logs its own failures
        except Exception as exc:  # noqa: BLE001 - nothing that escapes a pass may end the loop
            kind = failure_kind(exc)
            log.log(kind.level, "scout pass crashed", exc_info=exc,
                    extra={"event": "scout_pass_failed", "failure_kind": kind.value})


def status(config: Config, db: sqlite3.Connection, repo: Repo) -> dict:
    if config.scout is None:
        raise ValueError("No [scout] section in cardinal.toml")
    undecided = [{"issue": row["issue"], "title": row["title"], "outcome": row["outcome"]}
                 for row in store.undecided(db, repo.slug)]
    record = outcomes.track_record(db, repo.slug)
    categories = {}
    for category in config.scout.categories:
        # A repro is judged per proposal, so assume one could succeed only when repro is enabled;
        # with repro off a pass can never file a bug or regression ready.
        mode, why = gate.decide(config.scout, record, category, repro_ok=config.scout.repro)
        categories[category] = {**record.get(category, {}), "gate": mode, "why": why}
    return {"repo": repo.slug, "autonomy": config.scout.autonomy, "window_days": WINDOW_DAYS,
            "categories": categories, "open": undecided,
            "held": [row["title"] for row in store.held(db, repo.slug)], "last_pass": store.last_pass(db, repo.slug)}
