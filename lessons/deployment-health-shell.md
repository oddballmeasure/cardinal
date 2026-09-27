Use a non-login shell for short, repeatable health checks.

The first fake-host E2E run timed out during shell startup before its tiny
health command executed. Running `bash -c` for the configured check avoids login
profile work and lets the test exercise the health result itself.
