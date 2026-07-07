"""
Unit tests for wrapper-codex runner.

Tests build_command, extract_text, extract_event_summary, validate_cwd against
codex-cli 0.141.0 flag surface (approval_policy uses -c, "never" uses bypass).
"""
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).parent / "runner.py"
spec = importlib.util.spec_from_file_location("codex_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner)

CodexRunSpec = runner.CodexRunSpec
build_command = runner.build_command
extract_text = runner.extract_text
extract_event_summary = runner.extract_event_summary
validate_cwd = runner.validate_cwd


def test_build_command_default_never_uses_bypass():
    spec = CodexRunSpec(
        run_id="r1",
        prompt="hello",
        cwd="/root",
        cli_bin="codex",
        model="gpt-5-codex",
        sandbox="workspace-write",
        approval_policy="never",
    )
    cmd = build_command(spec)
    assert cmd[:3] == ["codex", "exec", "--json"], cmd
    assert "-C" in cmd and "/root" in cmd, cmd
    assert "-m" in cmd and "gpt-5-codex" in cmd, cmd
    assert "--skip-git-repo-check" in cmd, cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd, cmd
    assert cmd[-1] == "-", cmd


def test_build_command_on_request_uses_config_override():
    spec = CodexRunSpec(
        run_id="r2",
        prompt="hi",
        cwd="/root",
        cli_bin="codex",
        approval_policy="on-request",
    )
    cmd = build_command(spec)
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd, cmd
    assert "-c" in cmd
    assert any('approval_policy="on-request"' in x for x in cmd), cmd


def test_build_command_unknown_policy_falls_back_to_never():
    spec = CodexRunSpec(
        run_id="r3",
        prompt="hi",
        cwd="/root",
        cli_bin="codex",
        approval_policy="bogus-unknown",
    )
    cmd = build_command(spec)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd, cmd


def test_build_command_ephemeral_flag():
    spec = CodexRunSpec(
        run_id="r4",
        prompt="hi",
        cwd="/root",
        cli_bin="codex",
        ephemeral=True,
    )
    cmd = build_command(spec)
    assert "--ephemeral" in cmd, cmd


def test_validate_cwd_allows_root():
    assert validate_cwd("/root", ["/root"]) == "/root"


def test_validate_cwd_rejects_outside():
    try:
        validate_cwd("/tmp", ["/root"])
    except ValueError as e:
        assert "outside allowed roots" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_extract_text_thread_started_returns_empty():
    assert extract_text('{"type":"thread.started","thread_id":"abc"}') == ""


def test_extract_text_agent_message_returns_text():
    assert extract_text('{"type":"item.updated","item":{"type":"agent_message","text":"hello world"}}') == "hello world"


def test_extract_text_agent_message_completed_returns_text():
    assert extract_text('{"type":"item.completed","item":{"type":"agent_message","text":"bye"}}') == "bye"


def test_extract_text_command_execution_returns_empty():
    assert extract_text('{"type":"item.completed","item":{"type":"command_execution","command":"ls"}}') == ""


def test_extract_text_error_event_returns_empty():
    assert extract_text('{"type":"error","message":"oops"}') == ""


def test_extract_text_invalid_json_returns_empty():
    assert extract_text("not-json") == ""


def test_extract_event_summary_thread_started():
    assert extract_event_summary('{"type":"thread.started"}') == "thread.started"


def test_extract_event_summary_item_type():
    # Top-level "type" is preserved when present (more informative for metrics).
    assert extract_event_summary('{"type":"item.completed","item":{"type":"command_execution"}}') == "item.completed"


def test_extract_event_summary_invalid_returns_empty():
    assert extract_event_summary("not-json") == ""
    assert extract_event_summary('{"foo":1}') == ""
