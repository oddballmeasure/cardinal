import json

import pytest
from pydantic import ValidationError

from cardinal.contracts.deploy import DeployConfig, HealthConfig, HostEvidence
from cardinal.deploy.service import DeploymentService


class FakeHost:
    paced = False

    def __init__(self, stdout):
        self.stdout = stdout

    def execute(self, script):
        return HostEvidence(exit_code=0, stdout="deployed\n", stderr="")

    def check(self, remaining):
        return HostEvidence(exit_code=0, stdout=self.stdout, stderr="")


def service(expected, stdout):
    config = DeployConfig(host="fixture-host", user="deployer", working_directory="/srv/app",
                          deploy_timeout_seconds=3,
                          health={"command": "echo ok", "expected_stdout": expected,
                                  "timeout_seconds": 0.2, "interval_seconds": 0.05})
    svc = DeploymentService("a" * 40, "deploy/deploy.sh", b"#!/bin/sh\n", config, FakeHost(stdout))
    tools = {t.name: t for t in svc.tools()}
    tools["execute_deploy"].invoke({})
    return svc, json.loads(tools["check_health"].invoke({}))


def test_expected_stdout_with_trailing_newline_matches_and_whitespace_only_is_invalid():
    svc, first = service("ok\n", "ok\n")
    assert first["healthy"] is True, first
    assert svc.result().status == "healthy"

    _, wrong = service("ok", "not ok")
    assert wrong["healthy"] is False

    with pytest.raises(ValidationError):
        HealthConfig(command="echo ok", expected_stdout="  ", timeout_seconds=1, interval_seconds=1)


def test_expected_stdout_with_surrounding_spaces_matches_plain_output():
    svc, first = service("  ok  ", "ok\n")
    assert first["healthy"] is True, first
    assert svc.result().status == "healthy"
    assert svc.result().deployed_revision == "a" * 40
