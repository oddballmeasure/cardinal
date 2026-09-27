"""The deployment manager's tools: run the committed script once, then poll health until it is
healthy or the configured timeout passes. Host and commands are pinned; the model only sequences."""

import hashlib
import json
import math
import time

from langchain_core.tools import tool

from cardinal.contracts.deploy import DeployConfig, DeploymentResult, HealthObservation, HostEvidence


class DeploymentService:
    def __init__(self, revision: str, script_path: str, script: bytes, config: DeployConfig, host) -> None:
        self.revision, self.script_path, self.script_bytes = revision, script_path, script
        self.config, self.host = config, host
        self.paced = getattr(host, "paced", True)
        self.max_polls = math.ceil(config.health.timeout_seconds / config.health.interval_seconds) + 1
        self.script: HostEvidence | None = None
        self.observations: list[HealthObservation] = []
        self.events: list[dict] = []
        self.violations: list[str] = []
        self.health_started: float | None = None
        self.last_poll: float | None = None

    def _expired(self) -> bool:
        if len(self.observations) >= self.max_polls:
            return True
        return self.paced and self.health_started is not None and \
            time.monotonic() - self.health_started >= self.config.health.timeout_seconds

    def tools(self) -> list:
        @tool("execute_deploy")
        def execute_deploy() -> str:
            """Run the committed deploy script once on the configured host."""
            if self.script is not None:
                self.violations.append("deployment script executed more than once")
                return "error: the deployment already ran"
            self.script = self.host.execute(self.script_bytes)
            self.events.append({"action": "execute_deploy", "host": self.config.host,
                                "script_sha256": hashlib.sha256(self.script_bytes).hexdigest(), **self.script.model_dump()})
            if self.script.exit_code == 0:
                self.health_started = time.monotonic()
            return self.script.model_dump_json()

        @tool("check_health")
        def check_health() -> str:
            """Poll the configured health command once. Repeat until healthy or timed_out is true."""
            if self.script is None or self.script.exit_code != 0 or any(item.healthy for item in self.observations):
                self.violations.append("health checked before a successful deploy or after success")
                return "error: health check unavailable"
            if self._expired():
                self.violations.append("health checked after timeout")
                return "error: health polling limit reached"
            if self.paced and self.last_poll is not None:
                time.sleep(max(0.0, self.config.health.interval_seconds - (time.monotonic() - self.last_poll)))
            remaining = self.config.health.timeout_seconds
            if self.paced:
                remaining = max(0.01, remaining - (time.monotonic() - self.health_started))
            result = self.host.check(remaining)
            self.last_poll = time.monotonic()
            healthy = result.exit_code == 0 and result.stdout.strip() == self.config.health.expected_stdout
            observation = HealthObservation(**result.model_dump(), healthy=healthy)
            self.observations.append(observation)
            self.events.append({"action": "check_health", **observation.model_dump()})
            return json.dumps({**observation.model_dump(), "timed_out": not healthy and self._expired()})

        return [execute_deploy, check_health]

    def result(self) -> DeploymentResult:
        if self.script is None:
            raise ValueError("The deployment manager did not run the deploy script")
        if self.script.exit_code != 0:
            status = "script_failed"
        elif any(item.healthy for item in self.observations):
            status = "healthy"
        elif self._expired():
            status = "health_timeout"
        else:
            raise ValueError("The deployment manager stopped before health was healthy or timed out")
        return DeploymentResult(revision=self.revision, host=self.config.host, script_path=self.script_path,
                                status=status, script=self.script, health_observations=self.observations,
                                deployed_revision=self.revision if status == "healthy" else None)
