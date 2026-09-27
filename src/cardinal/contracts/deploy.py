from typing import Literal

from pydantic import BaseModel, Field


class HealthConfig(BaseModel):
    command: str = Field(min_length=1)
    expected_stdout: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    interval_seconds: float = Field(gt=0)


class DeployConfig(BaseModel):
    host: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
    user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    port: int = Field(default=22, ge=1, le=65535)
    working_directory: str = Field(min_length=1)
    deploy_timeout_seconds: float = Field(gt=0)
    health: HealthConfig


class HostEvidence(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class HealthObservation(HostEvidence):
    healthy: bool


class DeploymentResult(BaseModel):
    revision: str
    host: str
    script_path: str
    status: Literal["healthy", "script_failed", "health_timeout"]
    script: HostEvidence
    health_observations: list[HealthObservation]
    deployed_revision: str | None
