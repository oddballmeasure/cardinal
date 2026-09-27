Replay profile labels must be scoped to the fixture they describe.

Profiling the notes repository exposed a ledger-specific transaction label on
its active formatter because the replay model matched any `formatters.py` path.
Scoping those labels to `ledger/` matters: otherwise a passing profile can give
the orchestrator false context for a different repository.
