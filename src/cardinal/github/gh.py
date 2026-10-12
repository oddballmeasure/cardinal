"""The only way Cardinal talks to GitHub: the authenticated `gh` CLI."""

import json
import subprocess


# Closes every issue and PR body Cardinal writes, so readers can tell its work apart.
SIGNATURE = ('<img src="https://raw.githubusercontent.com/oddballmeasure/cardinal/main/cardinal.png" '
             'alt="Cardinal" width="16" align="top"> Generated with [Cardinal](https://github.com/oddballmeasure/cardinal)')


class GhError(RuntimeError):
    pass


def signed(body: str) -> str:
    return body if SIGNATURE in body else f"{body.rstrip()}\n\n{SIGNATURE}"


def gh(*args: str, timeout: int = 60) -> str:
    try:
        result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh {' '.join(args[:3])} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} failed ({result.returncode}): {result.stderr.strip()[-2000:]}")
    return result.stdout


def gh_json(*args: str, timeout: int = 60):
    return json.loads(gh(*args, timeout=timeout))
