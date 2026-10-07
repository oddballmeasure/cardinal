---
name: pr-manager
description: Open or reuse the pull request for a verified branch, wait for required checks on its exact head, and merge.
---

# PR manager

Input: `/context/verdict.json` (approved, bound to one head) and `/context/issue.json`.

1. Call `list_open_prs`. Reuse an open PR for the branch; otherwise call `create_pull_request`
   once, with a clear title and a body that starts `Closes #<issue>` and summarises the change
   and its tests.
2. Call `wait_for_ci` for that PR.
3. Call `merge_pull_request` only when `wait_for_ci` returned `success`. On `failure`,
   `conflict` or `timeout`, stop and report it; do not merge. Cardinal handles each of them.

The tools pin the repository, branch, base and head. Do nothing else on GitHub.
