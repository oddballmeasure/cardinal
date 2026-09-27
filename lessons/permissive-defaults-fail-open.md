A missing or empty value must refuse, never read as "allow everything" (ported from Fleet).

Fleet's signature bug recurred for months: `owned_files=[]` was falsy and granted verifiers
unrestricted writes; `extra="ignore"` silently discarded a config field; an unknown model fell
through to a default price and killed runs on phantom spend. None raised.

In Cardinal: config models use `extra="forbid"`; `paths_off_limits`, `required_checks`, and
`test_command` are required per repository, and an empty list is a decision the operator wrote
down, not a default. Ask of every lookup and permission check what happens when the value is
empty, missing, or unknown, and make that case raise.
