---
name: scout-repro
description: Write one repository test that fails today because of a confirmed bug, and passes once it is fixed.
---

# Scout repro

Input: `/context/proposal.json` (a confirmed bug) and the repository at `/repo/`, writable for
tests only. Output: one `ReproResult`.

1. Read the existing tests and copy their style: same folder, runner and way of driving the app.
2. Write one new test that states the proposal's acceptance criteria at the application's
   boundary (HTTP, CLI or UI), so it fails now and passes once the bug is fixed.
3. Call `run_repo_tests` and confirm the new test fails for the reason the proposal gives.
   If it passes, the bug did not reproduce: say so in `note`.

Change only test files. Do not fix the bug. Name the test exactly as the runner reports it.
