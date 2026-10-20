"""The only way Cardinal talks to GitHub: the authenticated `gh` CLI."""

import json
import re
import subprocess
import time


MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.25
# Only repeat reads and mutations that converge on the same state. A create, comment, merge
# or rerun can succeed server-side despite a failed CLI response; repeating it is unsafe.
RETRYABLE_COMMANDS = {
    ("auth", "status"), ("repo", "view"), ("issue", "view"), ("issue", "list"),
    ("issue", "edit"), ("label", "create"), ("pr", "view"), ("pr", "list"),
    ("api",),
}
# A 5xx code counts only in an HTTP status context ("HTTP 502", "HTTP/1.1 503", "502 Bad Gateway"):
# a bare number may be data, such as the issue number in a GraphQL not-found error.
TRANSIENT = re.compile(r"something went wrong|\bhttp(?:/\d(?:\.\d)?)?\s*(?:500|502|503|504)\b|bad gateway|"
                       r"internal server error|service unavailable|gateway timeout", re.IGNORECASE)
PERMANENT = re.compile(r"\b(?:403|404)\b|permission denied|forbidden|validation failed|"
                       r"unprocessable entity|could not resolve to", re.IGNORECASE)


def _retryable(args: tuple[str, ...]) -> bool:
    if args[:1] == ("api",):
        return not any(flag in args for flag in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"))
    return args[:2] in RETRYABLE_COMMANDS


def _transient(stderr: str) -> bool:
    # A permission/validation error must not be retried even if a proxy adds a 5xx line.
    return not PERMANENT.search(stderr) and bool(TRANSIENT.search(stderr))


# Closes every issue and PR body Cardinal writes, so readers can tell its work apart.
SIGNATURE = ('<img src="https://raw.githubusercontent.com/oddballmeasure/cardinal/main/cardinal.png" '
             'alt="Cardinal" width="16" align="top"> Generated with [Cardinal](https://github.com/oddballmeasure/cardinal)')


class GhError(RuntimeError):
    pass


def signed(body: str) -> str:
    return body if SIGNATURE in body else f"{body.rstrip()}\n\n{SIGNATURE}"


def gh(*args: str, timeout: int = 60) -> str:
    command = f"gh {' '.join(args[:3])}"
    safe = _retryable(args)
    attempts = MAX_ATTEMPTS if safe else 1
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            failure = f"timed out after {timeout}s"
            transient = True
            cause = exc
        else:
            if result.returncode == 0:
                return result.stdout
            failure = f"failed ({result.returncode}): {result.stderr.strip()[-2000:]}"
            transient = _transient(result.stderr)
            cause = None
        if not transient or attempt == attempts:
            detail = (f"after {attempt} attempts (transient failure; retries exhausted)" if transient and safe
                      else "without retry (the operation may already have applied)" if transient else "")
            error = GhError(f"{command} {failure} {detail}".strip())
            if cause is not None:
                raise error from cause
            raise error
        time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def gh_json(*args: str, timeout: int = 60):
    return json.loads(gh(*args, timeout=timeout))
