Pin the observed remote head before resetting a test branch.

A cleaner that force-pushes a base commit without an exact lease can overwrite
work that arrived after the reset request was prepared. Checking the expected
head and using `--force-with-lease` makes stale requests fail before issue and
PR state is changed.
