"""Controlled client event/configuration checks; no live inference or CLI calls."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_read_qualification", ROOT / "tests/e2e_runtime/live_agent_read_qualification.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
pytestmark = pytest.mark.scope_policy

CLIENTS = ("claude", "codex", "opencode")
NONCE = "qualification-control-7281993"


def events(client, fixture, *, nonce=NONCE, final=None):
    final = nonce if final is None else final
    if client == "claude":
        return [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": str(fixture)}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "read-1", "content": [{"type": "text", "text": "1: " + nonce}], "is_error": False}]}},
            {"type": "result", "subtype": "success", "result": final, "is_error": False},
        ]
    if client == "codex":
        return [
            {"type": "item.started", "item": {"id": "read-1", "type": "command_execution", "command": "/bin/bash -lc 'cat -- qualification.txt'", "status": "in_progress"}},
            {"type": "item.completed", "item": {"id": "read-1", "type": "command_execution", "command": "/bin/bash -lc 'cat -- qualification.txt'", "status": "completed", "exit_code": 0, "aggregated_output": nonce + "\n"}},
            {"type": "item.completed", "item": {"id": "answer-1", "type": "agent_message", "text": final}},
            {"type": "turn.completed"},
        ]
    return [
        {"type": "tool_use", "part": {"id": "part-1", "callID": "read-1", "type": "tool", "tool": "read", "state": {"status": "completed", "input": {"filePath": str(fixture)}, "output": "<content>" + nonce + "</content>"}}},
        {"type": "text", "part": {"text": final}},
        {"type": "step_finish", "part": {"reason": "stop"}},
    ]


def parse(client, fixture, payload, **changes):
    result = {"returncode": 0, "timed_out": False, "latency_sec": 0,
              "stdout": "\n".join(json.dumps(event) for event in payload)}
    result.update(changes)
    return helper.parse_read_result(client, result, NONCE, fixture)


@pytest.mark.parametrize("client", CLIENTS)
def test_completed_read_and_nonce_answer_prove_actual_client_tool_loop(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    result = parse(client, fixture, events(client, fixture))
    assert result["passed"] is True
    assert result["tool_execution_observed"] is True
    assert result["successful_read_tool_events"] == 1
    assert result["terminal_event_observed"] is True
    assert NONCE not in json.dumps(result)
    assert "stdout" not in result


@pytest.mark.parametrize("client", CLIENTS)
def test_final_nonce_without_completed_read_tool_is_not_proof(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    payload = events(client, fixture)
    if client == "claude":
        payload = payload[-1:]
    elif client == "codex":
        payload = payload[-2:]
    else:
        payload = payload[-2:]
    result = parse(client, fixture, payload)
    assert result["answer_nonce_matches"] is True
    assert result["terminal_event_observed"] is True
    assert result["tool_execution_observed"] is False
    assert result["passed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_read_request_without_successful_tool_result_is_not_execution(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    payload = events(client, fixture)
    if client == "claude":
        payload[1]["message"]["content"][0]["is_error"] = True
    elif client == "codex":
        payload[1]["item"]["exit_code"] = 1
    else:
        payload[0]["part"]["state"]["status"] = "error"
    result = parse(client, fixture, payload)
    assert result["passed"] is False
    assert result["tool_execution_observed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_read_output_nonce_is_required_even_when_final_answer_matches(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    result = parse(client, fixture, events(client, fixture, nonce="different-content", final=NONCE))
    assert result["answer_nonce_matches"] is True
    assert result["tool_execution_observed"] is False
    assert result["passed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_terminal_errors_timeout_and_nonzero_exit_never_pass(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    for flags in ({"returncode": 1}, {"timed_out": True}):
        assert parse(client, fixture, events(client, fixture), **flags)["passed"] is False
    payload = events(client, fixture)
    if client == "claude":
        payload[-1].update({"subtype": "error_during_execution", "is_error": True})
    elif client == "codex":
        payload[-1] = {"type": "turn.failed"}
    else:
        payload.append({"type": "error"})
    result = parse(client, fixture, payload)
    assert result["client_error_observed"] is True
    assert result["passed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_non_read_tool_or_wrong_file_invalidates_otherwise_valid_loop(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    payload = events(client, fixture)
    if client == "claude":
        payload.insert(0, {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "bad", "name": "Bash", "input": {"command": "echo wrong"}}]}})
    elif client == "codex":
        payload.insert(0, {"type": "item.completed", "item": {"id": "bad", "type": "file_change"}})
    else:
        payload.insert(0, {"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"status": "completed"}}})
    result = parse(client, fixture, payload)
    assert result["successful_read_tool_events"] == 1
    assert result["forbidden_tool_events"] > 0
    assert result["passed"] is False
    wrong = events(client, tmp_path / "another-file.txt")
    if client == "codex":
        for event in wrong[:2]:
            event["item"]["command"] = "cat another-file.txt"
    assert parse(client, fixture, wrong)["passed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_malformed_tool_arguments_are_rejected_without_coercion(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    payload = events(client, fixture)
    if client == "claude":
        payload[0]["message"]["content"][0]["input"]["file_path"] = {"path": str(fixture)}
    elif client == "codex":
        for event in payload[:2]:
            event["item"]["command"] = ["cat", "qualification.txt"]
    else:
        payload[0]["part"]["state"]["input"]["filePath"] = {"path": str(fixture)}
    result = parse(client, fixture, payload)
    assert result["forbidden_tool_events"] > 0
    assert result["tool_execution_observed"] is False
    assert result["passed"] is False


def test_unknown_codex_action_is_rejected_even_with_successful_read(tmp_path):
    fixture = tmp_path / "qualification.txt"
    payload = events("codex", fixture)
    payload.insert(0, {"type": "item.completed", "item": {"id": "unsupported", "type": "dynamic_tool_call"}})
    result = parse("codex", fixture, payload)
    assert result["successful_read_tool_events"] == 1
    assert result["forbidden_tool_events"] == 1
    assert result["passed"] is False


@pytest.mark.parametrize("client", CLIENTS)
def test_nonce_in_answer_can_have_formatting_but_must_match(tmp_path, client):
    fixture = tmp_path / "qualification.txt"
    result = parse(client, fixture, events(client, fixture, final="`" + NONCE + "`"))
    assert result["passed"] is True and result["answer_exact"] is False
    assert parse(client, fixture, events(client, fixture, final="unrelated answer"))["passed"] is False


@pytest.mark.parametrize("command", ["cat qualification.txt", "cat -- qualification.txt", "/bin/cat qualification.txt", "/usr/bin/cat -- qualification.txt", "/bin/bash -lc 'cat -- qualification.txt'"])
def test_only_single_known_cat_read_is_accepted(tmp_path, command):
    assert helper._safe_cat(command, tmp_path / "qualification.txt") is True


@pytest.mark.parametrize("command", ["cat other.txt", "cat qualification.txt other.txt", "cat qualification.txt > output.txt", "cat qualification.txt; touch output.txt", "cat qualification.txt | tee output.txt", "../cat qualification.txt", "/tmp/cat qualification.txt", "/tmp/bash -lc 'cat qualification.txt'", "python -c 'print(1)'", "cat ../qualification.txt", "cat $(printf qualification.txt)"])
def test_arbitrary_commands_cat_binaries_and_paths_are_rejected(tmp_path, command):
    assert helper._safe_cat(command, tmp_path / "qualification.txt") is False


@pytest.mark.parametrize("client", CLIENTS)
def test_client_configuration_restricts_tools_auth_and_mutations(tmp_path, monkeypatch, client):
    monkeypatch.setenv("UNRELATED_API_KEY", "inherited-key-must-not-reach-client")
    fixture = tmp_path / "qualification.txt"
    fixture.write_text(NONCE)
    command, env = helper.client_command(client, "http://127.0.0.1:19999", "explicit-model", "dummy-local-token", tmp_path, fixture)
    assert NONCE not in " ".join(command)
    assert "dummy-local-token" not in " ".join(command)
    assert "UNRELATED_API_KEY" not in env
    assert env.get("HOME") == os.environ.get("HOME")
    assert env.get("CODEX_HOME") == os.environ.get("CODEX_HOME")
    if client == "claude":
        assert command[command.index("--tools") + 1] == "Read"
        assert command[command.index("--allowedTools") + 1] == f"Read(/{fixture})"
        assert "--bare" in command and "--safe-mode" in command and "--strict-mcp-config" in command
        assert "--no-session-persistence" in command
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "1024"
    elif client == "codex":
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command and "--ignore-rules" in command and "--ephemeral" in command
        configs = {command[index + 1].split("=", 1)[0]: json.loads(command[index + 1].split("=", 1)[1])
                   for index, argument in enumerate(command[:-1]) if argument == "-c"}
        assert configs["features.shell_tool"] is True
        assert configs["features.hooks"] is False and configs["features.apps"] is False and configs["features.plugins"] is False
        assert configs["features.multi_agent"] is False and configs["mcp_servers"] == {}
        assert configs["shell_environment_policy.inherit"] == "none"
        assert configs["shell_environment_policy.experimental_use_profile"] is False
        assert configs["model_context_window"] == 8192
        assert "cat -- qualification.txt" in command[-1]
    else:
        config = json.loads((tmp_path / "opencode.json").read_text())
        assert config["permission"] == {"*": "deny", "read": {"*": "deny", str(fixture): "allow", os.path.relpath(fixture, "/"): "allow"}}
        assert "tools" not in config["agent"]["qualification_reader"]
        assert config["agent"]["qualification_reader"]["permission"] == config["permission"]
        assert config["mcp"] == {} and config["plugin"] == []
        assert config["small_model"] == "wrapper_active/explicit-model"
        assert config["provider"]["wrapper_active"]["models"]["explicit-model"]["limit"]["output"] == 1024
        assert "--pure" in command
        assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "true"
        assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))


@pytest.mark.parametrize("client", CLIENTS)
def test_task_nonce_is_confined_to_fixture_and_never_reported(tmp_path, monkeypatch, client):
    remembered = {}

    def controlled_process(command, env, directory, timeout):
        fixture = directory / "qualification.txt"
        nonce = fixture.read_text().strip()
        remembered["directory"] = directory
        remembered["nonce"] = nonce
        assert nonce not in " ".join(command)
        payload = events(client, fixture, nonce=nonce)
        return {"returncode": 0, "timed_out": False, "latency_sec": 0,
                "stdout": "\n".join(json.dumps(event) for event in payload)}

    monkeypatch.setattr(helper, "run_process", controlled_process)
    result = helper.qualify_agent_read(client, "http://127.0.0.1:19999", "explicit-model", "dummy-token", 10)
    assert result["passed"] is True and result["fixture_unchanged"] is True
    assert remembered["nonce"] not in json.dumps(result)
    assert not remembered["directory"].exists()


def test_pending_requested_wrapper_is_rejected_before_any_environment_or_client_access(tmp_path, monkeypatch):
    metadata = {"wrappers": {"alpha": {"status": "production", "port": 19999, "client_qualification": {"verified_model": "explicit-model"}},
                             "blackbox": {"status": "pending", "port": 19998}}}
    (tmp_path / "wrappers.json").write_text(json.dumps(metadata))
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["agent-read", "--wrapper", "alpha", "--wrapper", "blackbox", "--output", str(tmp_path / "report.json")])
    visited = []
    monkeypatch.setattr(helper, "installed_cli_version", lambda name: visited.append(name))
    import dotenv
    monkeypatch.setattr(dotenv, "dotenv_values", lambda *args, **kwargs: visited.append("env"))
    with pytest.raises(SystemExit) as error:
        helper.main()
    assert error.value.code == 2
    assert visited == []
    assert not (tmp_path / "report.json").exists()
