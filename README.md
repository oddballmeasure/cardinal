# Cardinal

Cardinal resolves GitHub issues in repositories it does not own. One orchestrator call decides
whether to take an issue and splits it into tickets; coders implement them in an isolated
worktree against the repository's own tests; a verifier judges the branch at one exact commit;
a PR manager merges only after the required checks pass on that commit; an optional deployment
manager deploys the merge and waits for health.

The repository holds two packages:

| Package | Role |
|---|---|
| `src/cardinal` | The product: the `cardinal` CLI, graph, store, daemon, agents and skills |
| `src/cardinal_harness` | The grader: drives `cardinal` from outside and checks what it did |

The product never imports the harness or reads its acceptance tests
(`lessons/product-never-sees-the-oracle.md`).

## Using the product

```sh
uv sync --locked --extra openai
export OPENAI_API_KEY=...                  # the product reads keys from the environment only
uv run cardinal init --repo owner/name     # writes ~/.cardinal/cardinal.toml; edit it
uv run cardinal repos check                # gh auth, remote, base branch, labels, test command, keys
uv run cardinal run 42                     # one issue, in the foreground
uv run cardinal daemon --once              # every issue labelled cardinal:ready, in number order
uv run cardinal status 42 --json           # runs, events, agent calls and token use
uv run cardinal resume 42 --approve --note "..."   # answer a run waiting for a person (or --reject)
```

`$CARDINAL_HOME` (default `~/.cardinal`) holds `cardinal.toml`, `store.db` (what happened),
`checkpoints.db` (what a paused run does next), shared clones, per-issue worktrees and run
context. Every repository decision in the config is explicit: `test_command`,
`required_checks` and `paths_off_limits` have no defaults, and unknown keys are refused.

### How a run flows

```
profile → intake ─┬─ needs a person → pause (cardinal resume) → intake
                  ├─ question / reject → settle
                  └─ tickets → implement ⇄ verify → publish → PR → deploy? → done
```

- **Profile.** It is stored per repository. Only files whose content hash changed are profiled again.
- **Intake** is one orchestrator call returning an `IntakeDecision`: the issue's kind, its requirements, and 1–4 tickets. It covers both triage and planning.
- **Implement.** Each ticket gets up to `coder_attempts` attempts, each judged by the repository's `test_command`. An accepted ticket is a commit carrying a `Cardinal-Ticket:` trailer, so a restarted run skips work already committed.
- **Verify.** The verifier's assessment is checked against facts the runtime gathers itself: tests at the head commit, test files changed, and off-limits paths untouched. A rejection sends the verifier's findings back as a repair ticket, up to `verify_rounds` times.
- **PR.** The branch is pushed with a lease. The PR merges only after every `required_checks` check passes on the verified head.
- **Labels.** Labels form one state axis: `cardinal:ready`, `in-progress`, `done`, `error` and `needs-human`. A failure records a typed `FailureKind`. Retryable failures are re-queued after `retry_after_hours` (at least 24) only when that setting is present.

Agents have no shell. The worktree is the only writable mount. Within it, `.git`, vendor
directories and `paths_off_limits` refuse writes, and the skills and run context are
read-only.

## Grading the product

```sh
uv run --locked python -m pytest -q                              # offline scenarios, no network
uv run --locked python -m cardinal_harness offline --scenario single      # or daemon | ci-failure | cleaner
uv run --locked --extra openai --extra e2e python -m cardinal_harness live --model openai:gpt-6-sol
```

The offline scenarios run the real product against a bare Git remote. `gh` is replaced by a
fake built from the real CLI's surface, models are scripted through `CARDINAL_MODEL_PROVIDER`,
and the deploy host is replaced through `CARDINAL_DEPLOY_HOST`.

Before grading, the live suite clones `https://github.com/oddballmeasure/cardinal_test_repo` into
`tests/blank_repo/` if that checkout is missing. An existing checkout is kept when its
`origin` matches; offline scenarios do not need this network step.

The live suite runs the product's daemon against `oddballmeasure/cardinal_test_repo`, one case
at a time and in suite order:

1. Park every ready issue.
2. Prove the case's acceptance tests fail on the current `main`.
3. Label the case ready and run `cardinal daemon --once`.
4. Grade the run from GitHub, Git refs, `status --json`, and the acceptance tests run cumulatively on the new `main`.

Cleanup always restores the pinned `main` under an exact lease, removes managed PRs and
branches, and restores every parked issue's labels. Every run writes
`artifacts/e2e/<run-id>/report.json` with a rerun command, the product's invocation logs and a
copy of its store.

`tests/blank_repo` is the local copy of the live test repository, a FastAPI, Redis and React
notes service with its own Docker-based HTTP and browser tests. `tests/acceptance` holds the
independent checks for each suite issue.
