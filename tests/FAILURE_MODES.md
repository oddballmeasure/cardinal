# First scenario failure modes

The acceptance checks live outside the agent-editable repository copy. They must
fail on the fixture baseline and pass only when the behavior in issue 104 works.

- `--since` is ignored, exclusive, or compares non-normalized date strings.
- An impossible date is accepted or fails without a useful CLI error.
- CSV output omits its header, drops Unicode, or fails to quote a comma in a field.
- An empty CSV result is missing the header or contains a data row.
- `--since` and CSV work separately but fail when used together.
- Ticket output omits an issue requirement, refers to a different issue, repeats an ID,
  or declares an unknown or cyclic dependency.
- The coder reports success without editing the repository or without passing the
  independent acceptance checks.
- A stage depends on a previous stage's transient state instead of its explicit input.
- A failed run exits without an artifact that explains what ran and why it failed.
- The verifier accepts a branch whose repository tests pass but whose behavior
  fails the issue's independent end-to-end checks.
- The verifier approves code without new repository tests, or approves a verdict
  tied to a different branch head than the code it checked.
- The PR manager creates a duplicate PR for an existing branch, merges while CI
  is pending or failed, or merges a revision different from the verifier's.
- A passing CI result for an older commit is treated as passing for the current
  branch head.
- The profiler omits tracked files, invents paths, or gives the active formatter's
  job to an unused legacy CSV helper.
- A large repository inventory is passed to one model call rather than being
  profiled in bounded batches.
- A tracked symlink is followed outside the repository, or a long file is loaded
  into the profiler's context without a bound.
- The orchestrator creates date or CSV tickets without consulting a profile for
  the current repository contents, or points coders at unrelated files.
- Deployment reads an unmerged working-tree script or a mixed/ambiguous deploy
  layout instead of the script and configuration from one merged revision.
- A deployment script fails, but the manager still runs health checks or reports
  a deployed revision.
- A deployment reports success on script exit alone, on the wrong host, on a
  transient health result, or after health never reaches the configured output.
- The combined flow deploys a PR that did not merge after CI, or a failed
  deployment lacks repeatable command and health evidence.
- A combined run reports passing stages whose repository revisions differ:
  tickets, coded diff, verified branch, merged main, or deployed revision.
- The cleaner resets a different branch or overwrites an unexpected remote head,
  then reopens an issue as if the reset succeeded.
- A reset leaves a managed feature branch or open PR behind, creates an issue
  when reopening was requested, or claims that a merged PR was unmerged.
- A cleanup report says the remote is clean without reading the remote ref and
  issue/PR state after the mutations.

Offline replay proves the wiring and checks; it cannot prove that a live model
reasoned correctly. The live mode is evaluated against the same acceptance checks.

## Notes service scenario

The independent HTTP checks for `tests/blank_repo` must expose the issue's missing
behavior on its baseline while the repository's own HTTP checks pass.

- A tag filter is ignored, matches only the original case, matches a substring,
  or changes creation order.
- CSV output is labeled JSON, loses Unicode, fails to quote a comma in a title,
  or serializes tags differently from the issue's semicolon-separated contract.
- A filtered CSV response with no notes omits its header or includes a data row.
- Tag filtering and CSV export work separately but not in the same request.
- The new query options change the default JSON response or note creation.
- The deploy script exits successfully without leaving state that the configured
  health command can verify on the same fake host.

## Docker and Redis notes repository

- The API reports healthy while Redis is unavailable, or Redis is not its source
  of truth for note and diary data.
- Notes disappear after restarting only the API container, or their returned
  order differs from creation order.
- Compose starts a web/API container without waiting for the Redis/API health
  dependency, publishes Redis unnecessarily, or leaves a volume behind after an
  acceptance run.
- The production web image serves a development server, fails its SPA fallback,
  or cannot proxy `/api` to the API service.
- The note form fails to submit title/tags to the existing API, splits tags
  incorrectly, or leaves the rendered list stale.
- Diary creation accepts missing/invalid fields, returns entries out of order,
  or loses entries after an API-only restart.
- Diary creation works before reload but the page does not show the persisted
  entry after reload.
- Two independent pytest invocations reuse a Compose project or Redis volume and
  contaminate each other's results.
- The CI check called `http-e2e` skips image builds, Redis-backed API behavior,
  or browser checks, allowing a PR to merge without validating the full stack.

## Live GitHub suite

- An issue without `cardinal:ready` is selected, or a managed issue is processed twice.
- A missing CI check is treated as success, or a check for an older branch head
  permits a merge of new code.
- One issue passes alone but breaks a previously merged issue; the final fetched
  `main` must satisfy all independent HTTP checks together.
- A failed live run leaves a managed branch, open PR, issue label, or changed
  `main` behind while reporting a clean reset.
- The cleaner overwrites a remote head that changed outside this suite.
