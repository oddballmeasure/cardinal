"""Independent acceptance tests: the oracle the product never sees."""

import os
import re
import subprocess
import sys
from pathlib import Path

from cardinal_harness.contracts import CheckEvidence

ROOT = Path(__file__).resolve().parents[2]


def run_checks(repo: Path, requirement_ids: list[str], scenario: dict, acceptance: Path) -> CheckEvidence:
    selector = " or ".join(scenario["acceptance_tests"][item] for item in requirement_ids)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(acceptance), "-k", selector],
        cwd=ROOT, env={**os.environ, "CARDINAL_TARGET_REPO": str(repo)},
        text=True, capture_output=True, check=False, timeout=900,
    )
    match = re.search(r"(\d+) passed", result.stdout)
    return CheckEvidence(selector=selector, exit_code=result.returncode,
                         passed_tests=int(match.group(1)) if match else 0,
                         stdout=result.stdout, stderr=result.stderr)
