A settings object holding a key is not the same as a library seeing it (ported from Fleet).

Fleet loaded `.env` through pydantic-settings; LangSmith reads only `os.environ`, so tracing was
"configured", reported healthy, and sent nothing. It was diagnosed twice.

In Cardinal: the product reads provider keys from the process environment only. The harness
loads `.env` into `os.environ` before it launches the `cardinal` subprocess. Verify a credential
by a real call (the harness's model probe), not by its presence in config.
