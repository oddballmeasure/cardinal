Verifier findings must contain unresolved blockers only when approval depends on an empty list.

The first live verifier put passing observations in `findings` while setting all
requirements true. The runner interpreted them as blocking findings and rejected
a candidate whose independent HTTP and repository E2E checks passed. The skill,
schema, and request now state the empty-list approval contract explicitly.
