"""Exact-model Codex qualification metadata; no provider inference or CLI calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_read_catalog_helper", ROOT / "tests/e2e_runtime/live_agent_read_qualification.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
pytestmark = pytest.mark.scope_policy


def test_custom_catalog_preserves_exact_model_and_read_instructions(tmp_path):
    model = "provider/exact-model:free"
    instructions = "Read the requested file with /usr/bin/cat only.\n"
    path = helper._codex_read_catalog(tmp_path, model, instructions)
    catalog = json.loads(path.read_text())
    assert path.parent == tmp_path
    assert len(catalog["models"]) == 1
    entry = catalog["models"][0]
    assert entry["slug"] == model
    assert entry["display_name"] == model
    assert entry["upgrade"] is None
    assert entry["model_messages"]["instructions_template"] == instructions
    assert "used_fallback_model_metadata" not in entry


def test_custom_catalog_advertises_only_bounded_text_read_capabilities(tmp_path):
    entry = json.loads(helper._codex_read_catalog(tmp_path, "exact-model", "read only").read_text())["models"][0]
    assert entry["context_window"] == entry["max_context_window"] == 8192
    assert entry["shell_type"] == "unified_exec"
    assert entry["input_modalities"] == ["text"]
    assert entry["supported_reasoning_levels"] == []
    assert entry["default_reasoning_level"] is None
    assert entry["supports_reasoning_summary_parameter"] is False
    assert entry["default_reasoning_summary"] == "none"
    assert entry["support_verbosity"] is False
    assert entry["apply_patch_tool_type"] is None
    assert entry["tool_mode"] == "direct"
    assert entry["experimental_supported_tools"] == []
    assert entry["node_repl_disabled"] is True
    for field in ("include_apps_usage_instructions", "include_plugin_usage_instructions",
                  "include_skills_usage_instructions", "supports_search_tool",
                  "supports_experimental_context", "supports_reasoning_effort_updates", "use_responses_lite"):
        assert entry[field] is False


def test_codex_command_selects_temporary_catalog_and_absolute_read_without_secret_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-inherit")
    fixture = tmp_path / "qualification.txt"
    fixture.write_text("nonsecret-fixture-content")
    model = "provider/exact-model:free"
    command, env = helper.client_command("codex", "http://127.0.0.1:19999", model,
                                         "dummy-token", tmp_path, fixture)
    configs = {command[i + 1].split("=", 1)[0]: json.loads(command[i + 1].split("=", 1)[1])
               for i, value in enumerate(command[:-1]) if value == "-c"}
    catalog = json.loads(Path(configs["model_catalog_json"]).read_text())
    assert catalog["models"][0]["slug"] == command[command.index("--model") + 1] == model
    assert Path(configs["model_catalog_json"]).parent == tmp_path
    assert configs["model_instructions_file"] == str(tmp_path / "minimal-instructions.md")
    assert configs["features.shell_tool"] is True
    assert configs["features.unified_exec"] is False
    assert configs["shell_environment_policy.inherit"] == "none"
    assert "tools.view_image" not in configs
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "/usr/bin/cat -- qualification.txt" in command[-1]
    assert "UNRELATED_API_KEY" not in env
    assert "dummy-token" not in " ".join(command)
    assert "dummy-token" not in json.dumps(catalog)
    assert fixture.read_text() not in " ".join(command)


@pytest.mark.parametrize("error_kind", ["error", "turn.failed", "item.completed"])
def test_catalog_does_not_relax_codex_error_acceptance(tmp_path, error_kind):
    fixture = tmp_path / "qualification.txt"
    nonce = "nonsecret-read-fixture"
    event = {"type": error_kind}
    if error_kind == "item.completed":
        event["item"] = {"id": "bad", "type": "error", "message": "unrecognized model metadata"}
    events = [
        {"type": "item.completed", "item": {"id": "read", "type": "command_execution",
            "command": "/usr/bin/cat -- qualification.txt", "status": "completed", "exit_code": 0,
            "aggregated_output": nonce}},
        event,
        {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": nonce}},
        {"type": "turn.completed"},
    ]
    result = helper.parse_read_result("codex", {"returncode": 0, "timed_out": False,
        "stdout": "\n".join(json.dumps(value) for value in events)}, nonce, fixture)
    assert result["successful_read_tool_events"] == 1
    assert result["client_error_observed"] is True
    assert result["passed"] is False
