Claude Code checks `--json-schema` in strict mode; pydantic's tuple schemas (`prefixItems`) are refused before the first turn.

The first live scout pass on `claude-code:sonnet` crashed every survey in about a second:
`ScoutFindings` and `ProposalReview` hold tuple fields, and the CLI exited with "unknown keyword:
prefixItems". The Deep Agents path never saw it because it uses a tool-call strategy, not the CLI's
validator.

In Cardinal: `claude_code.strict_schema` rewrites a tuple into a fixed-length array of its item
types; pydantic still validates the exact shape afterwards. A CLI failure carries the CLI's stderr in its
`provider` failure. When a contract gains a new shape, pass its schema to
`claude -p --json-schema` once before a live run.
