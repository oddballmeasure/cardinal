Parallel runs are processes sharing one clone, profile and base branch: lock what they share, and take merge_sha from the PR.

Since 2026-10-07 the daemon can run up to `max_parallel_runs` issues at once, one `cardinal run N`
process each (`daemon/workers.py`). Processes, not threads: each gets its own SQLite connection,
LangGraph thread pools (lessons/nested-agent-checkpointer-deadlock.md) and SIGTERM unwinding.

What the processes share, and how (`cardinal/locks.py`, flock files beside the clone):
- **Clone refs and worktree list** (`git` lock): fetch, worktree add/remove/prune and push all write
  the shared `.git`. Concurrent fetches fail with "cannot lock ref". Held for seconds only.
- **Planning** (`intake` lock): profile and intake run one issue at a time, so the single
  `repo_profiles` row is never written by two runs at once.
- **Merging** (`merge` lock): a sibling's merge can turn a PR `CONFLICTING` after its CI passed.
  Mergeability is checked again under the lock and a conflict goes back to sync, not to a failed
  `gh pr merge` (which would be a GITHUB failure).
- **merge_sha** is the PR's `mergeCommit.oid`. Reading the base head after merging recorded a
  sibling's merge whenever two landed close together.
- **Claims** carry host and pid. A killed process never releases its claim; the daemon settles
  claims whose pid is gone (`app.abandon`).
- **SIGTERM** is honoured once (`cli/main.py`): systemd's cgroup kill and the daemon's own
  forwarding both reach a worker, and a second SystemExit would cut its settle short.

Proven offline by the `parallel` and `worker-killed` scenarios (fake gh serialises its calls with
a lock, as GitHub's API is atomic). Not proven: live GitHub's mergeability timing under sibling
merges, and the load of several `test_command`s at once on a real host.
