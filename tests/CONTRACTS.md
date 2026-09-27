# Stage contracts

| Component | Input | Output | Skill | Status |
| --- | --- | --- | --- | --- |
| Management UI | Current repository/worker configuration and operator action | Updated configuration and visible result | `skills/management-ui/SKILL.md` | Contract only |
| Profiler | Local Git repository, repository ID, tracked files at one revision | One `RepoProfile` file per repository with revision, content hash, and file capabilities | `skills/profiler/SKILL.md` | Runnable |
| Orchestrator | GitHub-shaped issue and fresh repository profile | `TicketBatch`: issue number and 1–4 tickets with IDs, tasks, acceptance criteria, requirement IDs, dependencies, and target files | `skills/orchestrator/SKILL.md` | Runnable |
| Coder | One ticket, original issue, writable repository copy | Code and repository E2E test diff; `CodingResult` with changed files and independent test evidence | `skills/coder/SKILL.md` | Runnable |
| Verifier | Full issue, ticket batch, branch head, diff, repository tests, and independent issue checks | `VerificationResult`: approved or rejected for the exact head, requirement map, test evidence, and findings | `skills/verifier/SKILL.md` | Runnable |
| PR manager | Branch, matching approved verdict, existing PRs, and exact-head CI status | `PRResult`: PR URL, created/reused status, CI observations, and merged or blocked result | `skills/pr-manager/SKILL.md` | Runnable locally and on GitHub |
| Deployment manager | Merged Git revision with co-located `deploy.sh` and `deploy.json` | `DeploymentResult`: script evidence, health observations, status, and deployed revision only when healthy | `skills/deployment-manager/SKILL.md` | Runnable locally; opt-in SSH |
| Cleaner | Expected merged remote head, base SHA, managed PRs and branches, and issue actions with original labels | `CleanupResult`: verified remote base, no managed branches or open PRs, and restored ready issues | `skills/cleaner/SKILL.md` | Runnable with fake remote; opt-in GitHub |

For issue 104, the harness validates that tickets cover R1–R5 once, have unique IDs,
refer to issue 104, have known acyclic dependencies, and target profiled active
selection and formatting code. The profiler reads every tracked path in bounded
batches and rejects missing or invented entries. The coder's changed files
and test evidence come from the runner's observations, not its final message.
The verifier reruns repository E2E tests and independent issue acceptance tests;
the PR manager cannot merge a different branch head or a head without passing CI.
The deployment manager reads script and configuration from the merged Git object,
executes the script once, and polls the configured health command on the same
host. The combined flow deploys only after a successful merge.
The cleaner uses an exact remote-head lease before moving the target branch
back to its base commit. It closes only listed open PRs, keeps merged PRs as
history, removes listed remote feature branches, and restores the issue before
verifying the remote state from fresh reads.

The separate `tests/blank_repo` baseline is a notes HTTP service. The
`github-e2e` stage processes its easy, medium, and hard live issues against
the current GitHub `main`, checks each PR's verified branch SHA with the
`http-e2e` workflow, and restores the pinned main and issue labels afterward.
The offline combined runner remains tied to ledger issue 104.
