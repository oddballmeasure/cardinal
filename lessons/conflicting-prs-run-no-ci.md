GitHub runs no `pull_request` CI on a PR that conflicts with its base, so waiting for its checks can only time out.

On 2026-10-06 the daemon worked koizler #14 then #15. #14's PR merged while #15 was being coded, and
both appended tests to the end of the same file. #15's PR (#18) opened `CONFLICTING`/`DIRTY` with no
checks at all; `wait_for_ci` polled `pending` for 2400s and the run failed as `ci_timeout`, a wrong
`FailureKind` (oddballmeasure/cardinal#3). A person merged `main` in, keeping both blocks, and CI passed.

Cardinal now merges a moved base into the branch before publishing (the sync step), and
`wait_for_ci` reads `mergeable`: `CONFLICTING` stops the wait at once and goes back to sync, while
`UNKNOWN` (GitHub computes it lazily after a push) keeps polling. The offline fake models both, with
`git merge-tree` over the bare remote. A daemon working issues back to back always has a base that
moves under it.
