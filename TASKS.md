# Tasks

Plan: `~/.claude/plans/develop-a-plan-to-mellow-pinwheel.md`. Product is `src/cardinal`; the
harness in `src/cardinal_harness` grades it from outside. Mark `[x]` only with evidence.

## Phase 0 — setup
- [x] Port transferable Fleet lessons into `lessons/` (7 files)
- [x] `langgraph-checkpoint-sqlite`, `cardinal` script entry; `uv run cardinal --help` works

## Phase 1 — skeleton
- [x] config models (extra=forbid) + TOML load from `$CARDINAL_HOME`
- [x] `cardinal init`, `repos check`, `status --json`
- [x] Check: `repos check` passes against the live test repo (live run fbd524b9: all 11 probes ok)

## Phase 2 — GitHub, store, worktrees
- [x] `github/` one `gh` client: issues, label axis, PRs, required checks
- [x] `store/` schema, recorder (swallows, logs), inputs + runs (raise)
- [x] `repo/` shared clone, worktree per issue, publish with lease, ticket trailers
- [x] Fake `gh` over a bare remote (`cardinal_harness/fake_gh.py`); old `fake_github.py` removed

## Phase 3 — roles + graph via replay
- [x] Packaged skills; orchestrator merges triage; no fixture wording
- [x] Roles: incremental profiler, orchestrator/intake, coder, verifier, PR manager, deployer
- [x] CompositeBackend (worktree rw with vendor/off-limits refusals; skills + context ro)
- [x] Graph + runtime + FailureKind; `cardinal run`
- [x] Check: `python -m cardinal_harness offline --scenario single` passes (10/10)

## Phase 4 — daemon
- [x] `cardinal daemon --once`, number order, label settle, needs-human + `resume`
- [x] Check: `offline --scenario daemon` passes (12/12)
- [x] `resume --approve` path covered by issue #15 in the daemon scenario (16/16)

## Phase 5/6 — live through the product (deploy on from the start via the host hook)
- [x] Live smoke: `live --only easy` passed 6/6 checks + clean cleanup (artifact 9aa574c0; 11 agent calls, 324k input tokens)
- [x] Environment preflight: the test repo's own suite must pass at baseline before any model call
- [x] `artifacts/live.lock` stops two live suites sharing the test repo (happened once by accident; the second stopped before changing anything)
- [x] Full 6-case suite passing: run 4e94d0de, all 6 cases 6/6 checks, final cumulative acceptance green, cleanup clean (1.79M input tokens, ~33 min of agent time)
  - run c76ad9f4: easy passed; note-ui merged a working form but the hidden test required an unstated "Add note" label. Test now submits via the form's submit control (3/3 fail at baseline, 3/3 pass on PR #17). Other acceptance files audited: their unstated details (201, id, `error` key) are repository conventions
  - run b9f90c31: easy and note-ui both settled done (note-ui had never passed before), then the grader crashed re-cloning into case 1's acceptance folder; fixed with unique clones, and cumulative checks moved to one final pass
- [x] Moved out of the harness: runner.py, replay.py, fake_github.py, github_pr.py, deployment.py, profiler.py, github_e2e.py, role skills; dead fixtures removed
- [x] Live suite parks every ready issue and waits for label visibility (first smoke took #2: lessons/github-label-listing-lags.md)
- [x] Scoreboard: each live case appends to artifacts/scoreboard.jsonl keyed by product source hash

## Phase 7 — E2E tests
- [x] `tests/e2e/test_product.py` drives only the harness CLI: single, daemon, ci-failure, deploy, cleaner (+ live, opt-in)

## Phase 8 — logging, ingest, monitor (plan: ~/.claude/plans/now-is-the-time-drifting-floyd.md)
- [x] `LogRecord` v1 schema (`cardinal logs schema`), JSONL + SQLite sink that never raises, stdlib handler, run/stage context
- [x] Every catch site logs with its traceback: nodes, drive/settle, agent calls, daemon, probes, test timeouts, triage, monitor
- [x] `cardinal ingest`: token, per-record validation, configured repos only
- [x] `cardinal monitor`: fingerprint groups, thresholds, per-pass cap, findings dedup, recurrence, orchestrator triage to ready/investigate
- [x] Check: `offline --scenario monitor` passes 14/14; full offline suite 6 passed
- [ ] Live: monitor drafting and triage with a real model (not run yet)
- [x] Instrument `cardinal_test_repo`'s app to send records to ingest: `api/app/reporting.py`, baseline 65637e2
- [x] Example configs in `examples/` (cardinal.toml, app.env, log-record.json), checked by the offline monitor scenario
- [x] Live propagation (`python -m cardinal_harness propagate`): run 69ff64f9 passed 13/13, issue #29 → PR #30 merged, cleanup clean
- [x] Six-case live suite on baseline 65637e2: run 30d22d8e, 6/6 cases 6/6 checks, final acceptance green, cleanup clean (2.14M input tokens, ~34 min agent time)
- [x] Add `oddballmeasure/cardinal` under `[[repos]]` so Cardinal's own findings can be triaged and fixed: `~/.cardinal/cardinal.toml` gates merges on the new `offline-e2e` workflow (first run green, 2m38s); `repos check` all ok 2026-10-05
- [ ] Investigation agent for `cardinal:investigate`; log retention

## Phase 9 — Cardinal working on itself
- [x] `self-seed`: snapshot of Cardinal (minus tests/acceptance/self) in oddballmeasure/cardinal_self_test_repo, checkout tests/self_repo, baseline 3290aaf with green offline-e2e
- [x] Issues #1 (logs prune, easy) and #2 (logs query, medium); hidden tests fail 7/7 at baseline, pass 7/7 against a throwaway implementation
- [x] Live suite reads its target from the suite file; `live --suite tests/fixtures/self_suite.json`
- [x] Live self suite passing: run 6b0fe756, both cases 6/6 (PRs #5, #6), final acceptance 3/3 and 4/4, cleanup clean; 1.86M input / 45k output tokens for the run
- [x] (earlier) Live self suite run 9f95ad33: prune (#1) paused for approval as designed, harness approved, Cardinal coded and verified it, but offline-e2e failed on its new test (2 of 3 old records pruned on the runner; likely SQLite date() parsing differs by version). Query (#2) not reached
- [x] Live self suite, query (#2) alone: run caf2debf passed 6/6, PR #4 merged with green offline-e2e, hidden acceptance 4/4, cleanup clean (1.36M input tokens, 43 agent calls, ~17 min agent time)
- [x] CI repair loop: a failed required check's log goes back to the coder (ci_repair_rounds, default 1); offline ci-repair scenario
- [x] `paths_hidden`: agents can neither see nor change listed paths, so Cardinal's own grader can live in the repo it works on (d92496c, offline suite and CI green)
- [x] Cardinal resolved a real issue on its own repository: oddballmeasure/cardinal#1 (logs query), run 7c3688c588a1 by `daemon --once`, PR #2 merged after offline-e2e passed on head 34c4f89; hidden acceptance 4/4 fail at baseline d92496c and 4/4 pass on merge 1bb55c6. 40 agent calls (36 profiler batches for the first full profile), 1.40M input / 35k output tokens, 20 min wall clock, no pause, no repair round
- [ ] CI repair loop exercised on live CI (run 6b0fe756 passed CI first time, so the loop did not fire)
- [ ] Smoke the notes suite after the live.py refactor (`live --only easy`)

## Discovered along the way
- [x] Killed daemon left its test process group (and hung docker helpers) running: run_repo_tests now kills its group on any interruption; SIGTERM unwinds and settles the run as interrupted
- [x] Live grader crashed querying checks for an unpushed commit, and ran Docker acceptance for runs that never merged: both fixed
- [x] Docker Desktop credential helper hangs on this host (lessons/docker-credential-helper-hang.md): Docker Desktop restarted 2026-10-05; `docker pull hello-world` and `docker-credential-desktop get` both return promptly with the default config
- [ ] Verifier cannot tell an environment failure from a code failure; a repair round then chases the environment. Consider an environment-probe before blaming code
- [x] `AGENTS.md` dropped its agent-facing lines (the product ships `skills/CONVENTIONS.md`); it now points coding agents at `CLAUDE.md`
- [ ] Live `test_command` runs the harness interpreter (it has pytest + Playwright); a real operator needs the repo to own its test environment
- [ ] Product deploy for the notes repo only reports healthy through the harness's mock host; its committed health check needs a `ready` file that only the mock creates
