"""A controlled deploy host, served to the product through CARDINAL_DEPLOY_HOST.

It runs the committed script for real in a scratch directory. Health becomes possible only after
the fixture's `ready_after_polls` health checks, so a manager that stops at the first unhealthy
poll, or reports success on script exit alone, is caught.
"""

import json
import os
import subprocess
from pathlib import Path

from cardinal.contracts.deploy import DeployConfig, HostEvidence
from cardinal.deploy.hosts import evidence


class MockHost:
    paced = False

    def __init__(self, config: DeployConfig, fixture: dict, directory: Path) -> None:
        if fixture["host"] != config.host:
            raise ValueError(f"Configured deploy host {config.host} is not the available fake host {fixture['host']}")
        self.config, self.fixture = config, fixture
        self.directory = directory / config.host
        self.directory.mkdir(parents=True, exist_ok=True)
        self.polls = 0

    def _env(self) -> dict:
        return {**os.environ, "DEPLOY_HOST": self.config.host}

    def execute(self, script: bytes) -> HostEvidence:
        try:
            return evidence(subprocess.run(["bash", "-se"], input=script.decode(), cwd=self.directory, env=self._env(),
                                           text=True, capture_output=True, check=False,
                                           timeout=self.config.deploy_timeout_seconds))
        except subprocess.TimeoutExpired as exc:
            return evidence(exc)

    def check(self, timeout: float) -> HostEvidence:
        self.polls += 1
        ready_after = self.fixture["ready_after_polls"]
        if ready_after is not None and self.polls >= ready_after:
            (self.directory / "ready").touch()
        try:
            return evidence(subprocess.run(["bash", "-c", self.config.health.command], cwd=self.directory,
                                           env=self._env(), text=True, capture_output=True, check=False,
                                           timeout=max(timeout, 5)))
        except subprocess.TimeoutExpired as exc:
            return evidence(exc)


def factory(config: DeployConfig, settings, work_dir: Path) -> MockHost:
    fixture = json.loads(Path(os.environ["CARDINAL_MOCK_HOST_FIXTURE"]).read_text())
    return MockHost(config, fixture, work_dir)
