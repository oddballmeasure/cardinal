Exclude vendor and build output from agent searches before the result is built, not after (ported from Fleet).

One Fleet grep matched `frontend/node_modules` and returned 2,010,463 characters in a single tool
result; a 409 KB minified source-map line was among them. The recorder showed `truncated=1`, but
that clamp was display-only; the model still received all of it.

In Cardinal: the worktree backend hides vendor directories from `ls`, `glob`, and `grep`, and
tool output sent to the model is bounded per line and in total. A clamp in a log proves nothing
about the model's input.
