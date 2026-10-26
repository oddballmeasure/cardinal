"""A failed ready-queue poll in parallel mode must not stop the workers already running.

workers.parallel() is driven in-process with its process boundary replaced: each Worker is a stand-in
whose process records terminate() calls, and `issues.ready` raises GhError once while a worker runs."""

import logging
from types import SimpleNamespace

import pytest

from cardinal.config.models import Repo
from cardinal.daemon import workers
from cardinal.github.gh import GhError

SLUG = "team/app"


class FakeProcess:
    def __init__(self, polls_running: int) -> None:
        self.polls_running = polls_running
        self.returncode = None
        self.terminated = 0
        self.pid = 4242

    def poll(self):
        if self.returncode is None and self.polls_running > 0:
            self.polls_running -= 1
            return None
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _world(monkeypatch, ready, polls_running=2, view=None, sleep=None):
    started: list = []

    class FakeWorker:
        def __init__(self, home, repo, number, verbose) -> None:
            self.number = number
            self.process = FakeProcess(polls_running=polls_running)
            started.append(self)

        def result(self):
            if self.process.returncode == 0:
                return {"issue": self.number, "status": "done"}
            return {"issue": self.number, "status": "interrupted"}

    monkeypatch.setattr(workers, "Worker", FakeWorker)
    monkeypatch.setattr(workers.issues, "ready", ready)
    monkeypatch.setattr(workers.issues, "view",
                        view or (lambda slug, number: pytest.fail("issues.view must not reach gh")))
    monkeypatch.setattr(workers.post_merge, "judge_all", lambda repo, db: None)
    monkeypatch.setattr(workers.inputs, "orphaned_claims", lambda db, slug: [])
    monkeypatch.setattr(workers.time, "sleep", sleep or (lambda seconds: None))
    repo = Repo(slug=SLUG, base_branch="main", test_command=["true"], required_checks=[],
                paths_off_limits=[], paths_hidden=[], max_parallel_runs=2)
    return repo, started


def _poll_events(caplog):
    return [record for record in caplog.records if "poll" in str(getattr(record, "event", ""))]


def test_failed_ready_poll_keeps_running_worker_and_returns_its_result(monkeypatch, caplog):
    calls = {"n": 0}

    def ready(slug, label):
        calls["n"] += 1
        if calls["n"] == 1:
            return [5]
        if calls["n"] == 2:
            raise GhError("gh issue list failed: HTTP 502")
        return []

    repo, started = _world(monkeypatch, ready)
    caplog.set_level(logging.INFO)

    try:
        results = workers.parallel(None, repo, None, 0)
    except GhError as exc:
        results = exc

    assert len(started) == 1
    assert started[0].process.terminated == 0, (
        f"a failed poll SIGTERMed the running worker for issue #5 ({started[0].process.terminated} terminate call)")
    assert not isinstance(results, Exception), f"the poll failure escaped parallel(): {results!r}"
    assert results == [{"issue": 5, "status": "done"}], results
    assert calls["n"] >= 3, "the ready queue was not polled again after the failure"
    failed = _poll_events(caplog)
    assert len(failed) == 1, [getattr(record, "event", None) for record in caplog.records]


def test_failed_dependency_lookup_keeps_running_worker_and_retries(monkeypatch, caplog):
    calls = {"ready": 0, "view": 0}

    def ready(slug, label):
        calls["ready"] += 1
        if calls["ready"] == 1:
            return [5]
        if calls["ready"] == 2:
            return [6]
        return []

    def view(slug, number):
        calls["view"] += 1
        raise GhError("gh issue view failed: HTTP 502")

    repo, started = _world(monkeypatch, ready, polls_running=3, view=view)
    caplog.set_level(logging.INFO)

    try:
        results = workers.parallel(None, repo, None, 0)
    except GhError as exc:
        results = exc

    assert not isinstance(results, Exception), f"the lookup failure escaped parallel(): {results!r}"
    assert calls["view"] == 1
    assert [worker.number for worker in started] == [5], "an issue whose lookup failed was started anyway"
    assert started[0].process.terminated == 0
    assert results == [{"issue": 5, "status": "done"}], results
    assert [r for r in caplog.records if getattr(r, "event", None) == "dependency_lookup_failed"]
    assert not _poll_events(caplog)


def test_failed_dependency_lookup_is_retried_on_a_later_tick(monkeypatch):
    calls = {"ready": 0, "view": 0}

    def ready(slug, label):
        calls["ready"] += 1
        if calls["ready"] == 1:
            return [5]
        if calls["ready"] in (2, 3):
            return [5, 6]
        return [5]

    def view(slug, number):
        calls["view"] += 1
        if calls["view"] == 1:
            raise GhError("gh issue view failed: HTTP 502")
        return SimpleNamespace(body="independent work"), None, None

    repo, started = _world(monkeypatch, ready, polls_running=3, view=view)

    results = workers.parallel(None, repo, None, 0)

    assert calls["view"] == 2
    assert sorted(worker.number for worker in started) == [5, 6]
    assert all(worker.process.terminated == 0 for worker in started)
    assert sorted(result["issue"] for result in results) == [5, 6]
    assert all(result["status"] == "done" for result in results)


@pytest.mark.parametrize("where", ["ready", "sleep"])
def test_keyboard_interrupt_still_stops_every_running_worker(monkeypatch, where):
    calls = {"ready": 0, "sleep": 0}

    def ready(slug, label):
        calls["ready"] += 1
        if calls["ready"] == 1:
            return [5]
        if where == "ready":
            raise KeyboardInterrupt
        return []

    def sleep(seconds):
        calls["sleep"] += 1
        if where == "sleep" and calls["sleep"] == 2:
            raise KeyboardInterrupt

    repo, started = _world(monkeypatch, ready, polls_running=100, sleep=sleep)

    with pytest.raises(KeyboardInterrupt):
        workers.parallel(None, repo, None, 0)

    assert len(started) == 1
    assert [worker.process.terminated for worker in started] == [1]
