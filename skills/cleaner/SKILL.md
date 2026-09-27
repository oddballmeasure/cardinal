---
name: cleaner
description: Restore a pinned test repository branch and its managed GitHub issues and pull requests to a known starting state.
---

# Cleaner

## Inputs and output

Input: `/workspace/cleanup_request.json` with the repository, exact expected
remote head, target base commit, managed branch and PR IDs, and one or more issue
actions (`reopen` or `create`) with labels to restore. Output: a `CleanupResult`
verified against remote branch and issue/PR state. A merged PR remains historical.

## Tools

Read the request, then call `inspect_cleanup`, `reset_remote`,
`close_managed_prs`, `delete_managed_branches`, `restore_issue`, and
`verify_clean` in that order. The tools pin all refs and issue IDs to the
request. `reset_remote` uses an exact remote-head lease; do not substitute a
branch, commit, repository, or arbitrary shell command.

## Workflow

Stop if the remote head differs from the expected merged head or already
restored base. Close only listed open PRs and delete only listed managed
branches. Reopen or create each specified issue and restore its labels after the
remote reset succeeds. Report success only after `verify_clean` confirms the
base branch, managed branches, PRs, and issues from fresh reads.
