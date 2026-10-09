"""Drive the `cardinal` product from outside and grade what it did.

The harness only launches the product's CLI as a subprocess and reads back GitHub state, Git
refs, the product's own `status --json`, and independent acceptance tests the product never
sees. It never imports product internals to decide a pass.
"""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


def json_file(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(item) for item in items) + "]"


def write_config(home: Path, *, models: dict[str, str], slug: str, remote_url: str, test_command: list[str],
                 required_checks: list[str], paths_off_limits: list[str], deploy: str | None,
                 limits: dict[str, int] | None = None, source_repo: str = "oddballmeasure/cardinal",
                 sections: dict[str, dict] | None = None, extra_repos: list[str] | None = None,
                 max_parallel_runs: int | None = None, base_branch: str = "main") -> Path:
    home.mkdir(parents=True, exist_ok=True)
    lines = ["[models]", *(f"{role} = {json.dumps(spec)}" for role, spec in models.items()), "",
             "[logging]", 'level = "info"', f"source_repo = {json.dumps(source_repo)}", ""]
    for name, values in (sections or {}).items():
        lines += [f"[{name}]", *(f"{key} = {json.dumps(value)}" for key, value in values.items()), ""]
    if limits:
        lines += ["[limits]", *(f"{key} = {value}" for key, value in limits.items()), ""]
    lines += ["[[repos]]", f"slug = {json.dumps(slug)}", f"remote_url = {json.dumps(remote_url)}",
              f"base_branch = {json.dumps(base_branch)}", f"test_command = {toml_list(test_command)}",
              f"required_checks = {toml_list(required_checks)}", f"paths_off_limits = {toml_list(paths_off_limits)}",
              "paths_hidden = []"]  # graded targets never contain their own grader
    if max_parallel_runs is not None:
        lines.append(f"max_parallel_runs = {max_parallel_runs}")
    if deploy:
        lines += ["", "[repos.deploy]", f"transport = {json.dumps(deploy)}", 'local_directory = "deploy-host"']
    lines += extra_repos or []
    path = home / "cardinal.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def run_bounded(command: list[str], timeout: float, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with a timeout that holds: on expiry the whole process group is killed. A plain
    timeout kills only the child, then waits forever on pipes a grandchild still holds open."""
    process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        stderr += f"\nkilled with its process group after {timeout}s"
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class Product:
    """Runs `cardinal` with a fixed home and environment, keeping every invocation's output."""

    def __init__(self, home: Path, env: dict[str, str], artifact: Path) -> None:
        self.home, self.env, self.artifact = home, env, artifact
        self.invocations: list[dict] = []

    def __call__(self, *args: str, timeout: int = 7200) -> tuple[int, object]:
        command = [sys.executable, "-m", "cardinal.cli.main", "--home", str(self.home), "-v", *args]
        started = time.monotonic()
        result = run_bounded(command, timeout, env=self.env)
        index = len(self.invocations) + 1
        log = self.artifact / "product" / f"{index:02d}-{args[0]}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"$ {shlex.join(command)}\nexit {result.returncode}\n--- stdout ---\n{result.stdout}\n"
                       f"--- stderr ---\n{result.stderr}")
        try:
            payload = json.loads(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            payload = result.stdout
        self.invocations.append({"args": list(args), "exit": result.returncode,
                                 "seconds": round(time.monotonic() - started, 1), "log": str(log)})
        return result.returncode, payload

    def status(self, issue: int | None = None) -> list[dict]:
        code, payload = self("status", *([str(issue)] if issue is not None else []), "--json")
        if code != 0 or not isinstance(payload, list):
            raise ValueError(f"cardinal status failed ({code})")
        return payload

    def keep_store(self) -> None:
        for name in ("store.db", "cardinal.toml"):
            if (self.home / name).exists():
                shutil.copy2(self.home / name, self.artifact / name)


def product_env(extra: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("CARDINAL_")}
    env.update(extra)
    return env
