A track record that gates autonomy must count only decisions someone else made, or it confirms itself.

The scout reads a filed proposal's `cardinal:ready` label as a person's approval. In the first
offline run with `autonomy = "auto"` (scenario `scout`, artifact c25f2303), the next pass read
the bug the scout had itself filed ready as "approved", so every auto-filed issue would have
raised the approval rate that let it file ready. `scout/outcomes.py` now skips the ready/run rule
for `filed_mode = 'auto'`: those count only once a person closes them or the daemon lands them.
Apply the same test to any rate that grants Cardinal more freedom: which of its inputs did
Cardinal produce itself?
