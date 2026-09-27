Silencing an error makes a function claim success it did not achieve (ported from Fleet).

Fleet's worktree removal used `rmtree(ignore_errors=True)` then returned `True`; the directory
survived and the next run failed on an unrelated-looking "already exists" guard.

In Cardinal: the store's telemetry recorder may swallow its own write errors, because losing a
record must not lose a paid-for run. It logs every swallowed error. Inputs (daemon state, repo
profiles), git operations, and cleanup never swallow; a cleanup report is built from fresh reads.
