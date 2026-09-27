---
name: coder
description: Implement one ticket in the repository with boundary-level tests, and leave the repository's test command passing.
---

# Coder

Input: `/context/ticket.json`, `/context/issue.json`, `/context/requirements.json`, and
`/context/feedback.md` when an earlier attempt was not accepted. The repository is `/repo/`.

1. Read the ticket's `target_files`, then follow imports, callers and existing tests. The
   targets are a starting point, not a limit.
2. Implement the ticket. Preserve behavior outside it and match the repository's style.
3. Add or extend the repository's own tests so they exercise the new behavior through the
   application's boundary (HTTP requests, CLI invocations, or a browser), following how the
   existing tests start the application.
4. Call `run_repo_tests`. Fix every failure you caused and run it again. Finish only when it
   passes, or explain precisely what still fails.

Off-limits paths and vendor directories refuse writes. Cardinal commits your work and reruns
the test command itself; your account of the result never replaces that.
