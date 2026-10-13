"""Keep live qualification strict and distinguish backend failures from formatting."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def qualification():
    path = Path(__file__).parent / "e2e_runtime/live_client_qualification.py"
    spec = importlib.util.spec_from_file_location("live_qualification_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", [{"value": "OK"}, '{"value":"OK"}'])
def test_tool_arguments_require_the_declared_echo_input(qualification, value):
    assert qualification._arguments(value)


@pytest.mark.parametrize("value", [None, [], {}, "", "{", '{"value":"NO"}',
                                   {"value": "OK", "extra": "ignored"}])
def test_invalid_tool_arguments_cannot_pass_qualification(qualification, value):
    assert not qualification._arguments(value)


@pytest.mark.parametrize("answer,passed", [("OK", True), ("OK.", False), ("", False), ("NO", False)])
def test_claude_terminal_success_does_not_hide_literal_format_failure(qualification, answer, passed):
    result = {"returncode": 0, "timed_out": False, "latency_sec": 0,
              "stdout": json.dumps({"type": "result", "subtype": "success", "result": answer})}
    check = qualification.parse_cli_result("claude", result)
    assert check["terminal_event_observed"]
    assert check["passed"] is passed
    assert "stdout" not in check and "result" not in check


def test_client_child_environment_does_not_inherit_provider_secrets(qualification, monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-upstream-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-user-account-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://fake-proxy.invalid")
    env = qualification.client_environment("fake-local-token")
    assert env["WRAPPER_API_KEY"] == "fake-local-token"
    assert not {"NVIDIA_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY"} & env.keys()
