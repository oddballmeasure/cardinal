"""Where a deployment runs. `CARDINAL_DEPLOY_HOST=module:callable` substitutes the host, the same way
the model provider hook substitutes models; the callable receives (config, repo settings, work dir)."""

import importlib
import os
import shlex
import subprocess
from pathlib import Path

from cardinal.config.models import Deploy
from cardinal.contracts.deploy import DeployConfig, HostEvidence


def evidence(process: subprocess.CompletedProcess | subprocess.TimeoutExpired) -> HostEvidence:
    if isinstance(process, subprocess.TimeoutExpired):
        out = process.stdout.decode(errors="replace") if isinstance(process.stdout, bytes) else (process.stdout or "")
        err = process.stderr.decode(errors="replace") if isinstance(process.stderr, bytes) else (process.stderr or "")
        return HostEvidence(exit_code=124, stdout=out[-4000:], stderr=(err + "\ncommand timed out")[-4000:])
    return HostEvidence(exit_code=process.returncode, stdout=process.stdout[-4000:], stderr=process.stderr[-4000:])


class LocalHost:
    """Runs the committed script on this machine, in a configured directory."""

    paced = True

    def __init__(self, config: DeployConfig, directory: Path) -> None:
        self.config = config
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _run(self, argv: list[str], stdin: str, timeout: float) -> HostEvidence:
        try:
            return evidence(subprocess.run(argv, input=stdin, cwd=self.directory, text=True, capture_output=True,
                                           check=False, timeout=timeout,
                                           env={**os.environ, "DEPLOY_HOST": self.config.host}))
        except subprocess.TimeoutExpired as exc:
            return evidence(exc)

    def execute(self, script: bytes) -> HostEvidence:
        return self._run(["bash", "-se"], script.decode(), self.config.deploy_timeout_seconds)

    def check(self, timeout: float) -> HostEvidence:
        # Non-login shell: a login profile can outlast a short health timeout (lessons/deployment-health-shell.md).
        return self._run(["bash", "-c", self.config.health.command], "", timeout)


class SSHHost:
    paced = True

    def __init__(self, config: DeployConfig) -> None:
        self.config = config
        self.base = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                     "-p", str(config.port), f"{config.user}@{config.host}"]
        prefix = f"cd {shlex.quote(config.working_directory)} && DEPLOY_HOST={shlex.quote(config.host)} "
        self.script_command = prefix + "bash -se"
        self.health_command = prefix + "bash -c " + shlex.quote(config.health.command)

    def _run(self, remote: str, stdin: str, timeout: float) -> HostEvidence:
        try:
            return evidence(subprocess.run([*self.base, remote], input=stdin, text=True, capture_output=True,
                                           check=False, timeout=timeout))
        except subprocess.TimeoutExpired as exc:
            return evidence(exc)

    def execute(self, script: bytes) -> HostEvidence:
        return self._run(self.script_command, script.decode(), self.config.deploy_timeout_seconds)

    def check(self, timeout: float) -> HostEvidence:
        return self._run(self.health_command, "", timeout)


def host_for(config: DeployConfig, settings: Deploy, work_dir: Path):
    hook = os.environ.get("CARDINAL_DEPLOY_HOST")
    if hook:
        module_name, _, attribute = hook.partition(":")
        return getattr(importlib.import_module(module_name), attribute)(config, settings, work_dir)
    if settings.transport == "ssh":
        return SSHHost(config)
    if not settings.local_directory:
        raise ValueError("Local deploy transport requires local_directory")
    return LocalHost(config, Path(settings.local_directory).expanduser())
