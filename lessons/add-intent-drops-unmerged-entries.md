`git add -A -N` replaces a conflicted merge's unmerged index entries, so `git ls-files -u` and `--diff-filter=U` stop listing the conflicts.

Checked on git 2.43 on 2026-10-07: after a conflicting `git merge`, `git add -A -N` turned `UU f.txt`
into `DA f.txt`; `MERGE_HEAD` survived and the next `git add -A && git commit` still made a
two-parent merge commit. Cardinal's `fingerprint`, `changed_files` and `patch` all run `add -A -N`,
so anything that needs the conflicted files after the coder has run derives them from commits
instead: files changed on both sides since `merge-base HEAD MERGE_HEAD` that still hold markers
(`repo/git.py: unresolved`).
