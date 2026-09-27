"""The only way Cardinal talks to GitHub: the authenticated `gh` CLI."""

import json
import subprocess


class GhError(RuntimeError):
    pass


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
