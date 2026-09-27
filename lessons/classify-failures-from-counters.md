Decide why a run failed from hard facts before reading what it left behind (ported from Fleet).

Fleet reported "could not parse TriageVerdict" for a triage that had simply run out of turns,
and turned a provider context-overflow into `needs_human`, which has no retry path. The issue
sat silent for days.

In Cardinal: every terminal run carries a `FailureKind`. Recursion exhaustion, provider errors,
and a missing structured response are three different kinds. `needs_human` is reserved for an
orchestrator that explicitly asked for a person. Only retryable kinds get the `cardinal:error`
label and its retry clock.
