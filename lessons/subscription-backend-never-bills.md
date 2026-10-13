A `claude-code:` model must run on the Claude Code subscription; prove it on every call, never assume it.

The Claude Code CLI bills the API whenever `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN` is set,
even when an OAuth login is present, and `--bare` accepts only an API key. A daemon started from a
shell or unit that happens to export a key would quietly spend credits while appearing to work.

In Cardinal: `agents/claude_code.py` blanks both variables in the CLI's environment, never passes
`--bare`, and stops a session before its first turn unless its init message reports
`apiKeySource == "none"` (OAuth). Pay-as-you-go overage (`isUsingOverage`) is treated like a spent
usage window: the call waits for the reset instead of continuing. `cardinal repos check` makes one
real call per model and prints the subscription windows it used. Verified 2026-10-13 with a dummy
`ANTHROPIC_API_KEY` exported: the call still ran on OAuth.
