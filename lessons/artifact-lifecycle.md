Capture the repository patch before the temporary workspace is removed.

The first runner draft tried to collect the patch after leaving the temporary
directory context. That would make a failed coding run lose its most useful
evidence. Capture the patch inside the context's `finally` block so both passing
and failing runs retain it.
