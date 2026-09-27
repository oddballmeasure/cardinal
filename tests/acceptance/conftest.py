"""Start one disposable Docker Compose application for an acceptance run."""

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest


class ComposeStack:
    def __init__(self, repo: Path):
        self.repo = repo
        self.project = f"cardinal-accept-{uuid4().hex[:12]}"
        self.compose = ["docker", "compose", "-p", self.project, "-f", str(repo / "compose.yaml")]
        try:
            subprocess.run(
                [*self.compose, "up", "--build", "--detach", "--wait", "--wait-timeout", "240"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=True,
                timeout=360,
            )
            self.api_url = f"http://{self._host_port('api', 8000)}"
            self.web_url = f"http://{self._host_port('web', 80)}"
        except Exception:
            subprocess.run(
                [*self.compose, "down", "--volumes", "--remove-orphans"],
                cwd=repo,
                text=True,
                capture_output=True,
                check=False,
                timeout=90,
            )
            raise

    def _host_port(self, service: str, port: int) -> str:
        result = subprocess.run(
            [*self.compose, "port", service, str(port)],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        port = result.stdout.strip().splitlines()[-1].rsplit(":", 1)[-1]
        return f"127.0.0.1:{port}"

    def restart_api(self) -> None:
        subprocess.run([*self.compose, "restart", "api"], cwd=self.repo, check=True, timeout=60)
        subprocess.run(
            [*self.compose, "up", "--detach", "--wait", "--wait-timeout", "60", "api"],
            cwd=self.repo,
            check=True,
            timeout=90,
        )
        self.api_url = f"http://{self._host_port('api', 8000)}"

    def close(self) -> None:
        subprocess.run(
            [*self.compose, "down", "--volumes", "--remove-orphans"],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=True,
            timeout=90,
        )


@pytest.fixture(scope="session")
def stack() -> ComposeStack:
    repo = Path(os.environ["CARDINAL_TARGET_REPO"]).resolve()
    current = ComposeStack(repo)
    try:
        yield current
    finally:
        current.close()


@pytest.fixture(scope="session")
def service(stack: ComposeStack) -> str:
    return stack.api_url


@pytest.fixture(scope="session")
def web_url(stack: ComposeStack) -> str:
    return stack.web_url
