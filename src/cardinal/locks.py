"""Cross-process locks beside a repository's shared clone.

Parallel runs are separate processes that share one clone, one repo profile and one base branch.
`git`: anything that writes the clone's shared refs or worktree list (fetch, worktree add/remove,
push), held for seconds. `intake`: profile and intake, so one issue is planned at a time.
`merge`: the last mergeability check and the merge, so sibling PRs land one at a time.

Reentrant within a thread: create_worktree removes stale worktrees under the lock it already holds.
"""

import fcntl
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
_held = threading.local()


@contextmanager
def lock(clone: Path, name: str):
    depths = _held.__dict__.setdefault("depths", {})
    path = clone.parent / f"{clone.name}.{name}.lock"
    key = str(path)
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        started = time.monotonic()
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("waiting for the %s lock", name, extra={"event": "lock_wait", "data": {"lock": name}})
            fcntl.flock(handle, fcntl.LOCK_EX)
        waited = round(time.monotonic() - started, 3)
        log.info("holding the %s lock", name, extra={"event": "lock_acquired", "data": {"lock": name, "waited": waited}})
        depths[key] = 1
        try:
            yield
        finally:
            depths[key] = 0
            log.info("released the %s lock", name, extra={"event": "lock_released", "data": {"lock": name}})
            fcntl.flock(handle, fcntl.LOCK_UN)
