import subprocess

import pytest

from cardinal.github import gh as gh_module
from cardinal.github.gh import GhError, gh

NOT_FOUND = "GraphQL: Could not resolve to an issue or pull request with the number of 502. (repository.issue)\n"


def test_not_found_issue_number_502_is_permanent_and_not_retried(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=NOT_FOUND)

    monkeypatch.setattr(gh_module.subprocess, "run", fake_run)
    monkeypatch.setattr(gh_module.time, "sleep", lambda seconds: None)

    assert gh_module._transient(NOT_FOUND) is False
    assert gh_module._transient("HTTP 502: Bad Gateway (https://api.github.com/graphql)") is True

    with pytest.raises(GhError) as raised:
        gh("issue", "view", "502", "-R", "owner/name", "--json", "title")
    assert len(calls) == 1, f"permanent not-found error was retried: {len(calls)} invocations"
    assert "retries exhausted" not in str(raised.value)


@pytest.mark.parametrize("stderr", [
    "gh: HTTP 500",
    "HTTP 503: Service Unavailable",
    "HTTP/1.1 504",
    "502 Bad Gateway",
    "500 Internal Server Error",
    "504 Gateway Timeout",
])
def test_genuine_http_5xx_is_still_retried(monkeypatch, stderr):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    monkeypatch.setattr(gh_module.subprocess, "run", fake_run)
    monkeypatch.setattr(gh_module.time, "sleep", lambda seconds: None)

    with pytest.raises(GhError) as raised:
        gh("issue", "view", "7", "-R", "owner/name", "--json", "title")
    assert len(calls) == gh_module.MAX_ATTEMPTS
    assert "retries exhausted" in str(raised.value)


def test_bare_5xx_number_in_message_is_not_transient():
    assert gh_module._transient("pull request 500 has no commits\n") is False
