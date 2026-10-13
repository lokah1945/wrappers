#!/usr/bin/env python3
"""Opt-in live read-tool qualification of the three supported agent clients.

Each task reads a fresh temporary nonsecret nonce that is absent from the prompt.
Reports contain structural checks, never tool output, nonce, credentials or stderr.
This script does not start, restart or configure any backend service.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.wrapper_scope import WrapperScopeError, load_wrappers, select_wrappers
from live_client_qualification import CLI_PATHS, client_environment, installed_cli_version, run_process, source_fingerprints

KNOWN_EVENTS = {
    "claude": {"assistant", "user", "result", "system", "stream_event"},
    "codex": {"thread.started", "turn.started", "item.started", "item.updated", "item.completed",
              "turn.completed", "turn.failed", "error"},
    "opencode": {"step_start", "text", "tool_use", "step_finish", "error"},
}


def _events(stdout: str) -> list[dict]:
    try:
        value = json.loads(stdout)
    except ValueError:
        value = None
    if isinstance(value, dict):
        return [value]
    events = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(part) for part in value)
    if isinstance(value, dict):
        return _text(value.get("text", value.get("content", "")))
    return ""


def _same_file(value, expected: Path) -> bool:
    # Compare syntax only. Never resolve arbitrary client paths or inspect files.
    if not isinstance(value, str):
        return False
    candidate = os.path.normpath(value if os.path.isabs(value) else str(expected.parent / value))
    return candidate == os.path.normpath(str(expected))


def _safe_cat(command, expected: Path) -> bool:
    """Permit one cat invocation, optionally wrapped by the CLI's local shell."""
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
        shells = {name for basename in ("bash", "sh", "dash", "zsh")
                  for name in (basename, "/bin/" + basename, "/usr/bin/" + basename)}
        if len(args) == 3 and args[0] in shells and args[1] in {"-c", "-lc", "-cl"}:
            args = shlex.split(args[2])
    except ValueError:
        return False
    if not args or args[0] not in {"cat", "/bin/cat", "/usr/bin/cat"}:
        return False
    args = args[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    return len(args) == 1 and _same_file(args[0], expected)


def parse_read_result(client: str, result: dict, nonce: str, fixture: Path) -> dict:
    events = _events(result.get("stdout", ""))
    final_text = ""
    terminal = False
    successful_reads = 0
    forbidden = 0
    error = False
    if client == "claude":
        reads = {}
        completed_reads = set()
        for event in events:
            message = event.get("message", {})
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                content = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if event.get("type") == "assistant" and block.get("type") == "tool_use":
                    inputs = block.get("input", {})
                    safe = block.get("name") == "Read" and isinstance(inputs, dict) and _same_file(inputs.get("file_path"), fixture)
                    if safe and isinstance(block.get("id"), str):
                        reads[block["id"]] = True
                    else:
                        forbidden += 1
                elif event.get("type") == "user" and block.get("type") == "tool_result":
                    identifier = block.get("tool_use_id")
                    if identifier in reads and identifier not in completed_reads:
                        completed_reads.add(identifier)
                        if not block.get("is_error", False) and nonce in _text(block.get("content")):
                            successful_reads += 1
            if event.get("type") == "result":
                final_text = _text(event.get("result"))
                terminal = event.get("subtype") == "success" and not event.get("is_error", False)
                error |= not terminal
    elif client == "codex":
        completed_reads = set()
        for event in events:
            kind = event.get("type")
            terminal |= kind == "turn.completed"
            error |= kind in {"turn.failed", "error"}
            item = event.get("item", {})
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if kind == "item.completed" and item_type == "agent_message":
                final_text = _text(item.get("text"))
            if kind in {"item.started", "item.updated", "item.completed"} and item_type == "command_execution":
                safe = _safe_cat(item.get("command"), fixture)
                if not safe:
                    forbidden += 1
                identifier = item.get("id")
                if kind == "item.completed" and safe and identifier not in completed_reads:
                    completed_reads.add(identifier)
                    output = item.get("aggregated_output", item.get("stdout", ""))
                    if item.get("status") == "completed" and item.get("exit_code") == 0 and nonce in _text(output):
                        successful_reads += 1
            elif isinstance(item_type, str) and item_type not in {"agent_message", "reasoning", "command_execution"}:
                forbidden += 1
                error |= item_type == "error"
    elif client == "opencode":
        parts = []
        completed_reads = set()
        for event in events:
            kind = event.get("type")
            error |= kind == "error"
            part = event.get("part", {})
            if not isinstance(part, dict):
                continue
            if kind == "text":
                parts.append(_text(part.get("text")))
            if kind == "step_finish":
                terminal = part.get("reason") == "stop"
            if kind == "tool_use" or part.get("type") == "tool":
                state = part.get("state", {})
                state = state if isinstance(state, dict) else {}
                inputs = state.get("input", {})
                safe = part.get("tool") == "read" and isinstance(inputs, dict) and _same_file(inputs.get("filePath"), fixture)
                if not safe:
                    forbidden += 1
                identifier = part.get("callID", part.get("id"))
                if safe and state.get("status") == "completed" and identifier not in completed_reads:
                    completed_reads.add(identifier)
                    if not state.get("error") and nonce in _text(state.get("output")):
                        successful_reads += 1
        final_text = "".join(parts)
    else:
        raise ValueError("unsupported client")
    matched = bool(nonce) and nonce in final_text
    types = {event.get("type") for event in events if isinstance(event.get("type"), str)}
    return {
        "exit_code": result.get("returncode"), "timed_out": bool(result.get("timed_out")),
        "terminal_event_observed": terminal, "client_error_observed": error,
        "has_visible_answer": bool(final_text.strip()), "answer_nonce_matches": matched,
        "answer_exact": final_text.strip() == nonce, "visible_characters": len(final_text),
        "successful_read_tool_events": successful_reads, "tool_execution_observed": successful_reads > 0,
        "forbidden_tool_events": forbidden,
        "event_types": sorted(types & KNOWN_EVENTS[client]), "unknown_event_type_count": len(types - KNOWN_EVENTS[client]),
        "latency_sec": result.get("latency_sec"),
        "passed": result.get("returncode") == 0 and not result.get("timed_out") and terminal and not error
                  and matched and successful_reads > 0 and forbidden == 0,
    }


def _codex_read_catalog(state: Path, model: str, instructions: str) -> Path:
    """Declare the bounded read task's metadata for the exact custom model.

    Codex 0.160.1 loads a ModelsResponse with ModelInfo entries through
    model_catalog_json. These limits describe this qualification task, not the
    upstream model's maximum capacity or support for other coding workloads.
    """
    path = state / "codex-models.json"
    entry = {
        "slug": model, "display_name": model,
        "description": "Temporary bounded read-tool qualification",
        "supported_reasoning_levels": [], "default_reasoning_level": None,
        "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
        "priority": 0, "availability_nux": None, "upgrade": None,
        "model_messages": {"instructions_template": instructions},
        "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False,
        "include_apps_usage_instructions": False,
        "supports_reasoning_summary_parameter": False, "default_reasoning_summary": "none",
        "support_verbosity": False, "default_verbosity": None,
        "apply_patch_tool_type": None, "web_search_tool_type": "text",
        "truncation_policy": {"mode": "tokens", "limit": 1024},
        "context_window": 8192, "max_context_window": 8192,
        "experimental_supported_tools": [], "input_modalities": ["text"],
        "supports_search_tool": False, "supports_experimental_context": False,
        "supports_reasoning_effort_updates": False, "use_responses_lite": False,
        "node_repl_disabled": True, "tool_mode": "direct", "multi_agent_version": None,
    }
    path.write_text(json.dumps({"models": [entry]}) + "\n")
    return path


def client_command(client: str, origin: str, model: str, token: str, state: Path, fixture: Path) -> tuple[list[str], dict]:
    env = client_environment(token)
    command = [str(CLI_PATHS[client])]
    prompt = f"Read the file {fixture} exactly once using the read tool. Reply with its exact content only. Do not read other files or modify anything."
    if client == "claude":
        env.update({"ANTHROPIC_BASE_URL": origin, "ANTHROPIC_API_KEY": token, "ANTHROPIC_AUTH_TOKEN": token,
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
                    "MAX_THINKING_TOKENS": "0"})
        command += ["--bare", "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
                    "--disable-slash-commands", "--tools", "Read", "--allowedTools", f"Read(/{fixture})",
                    "--permission-mode", "dontAsk", "--no-session-persistence", "--system-prompt",
                    "Read the single requested file using Read, then answer only with its contents. Never use any other tool.",
                    "--model", model, "--output-format", "stream-json", "--verbose", "--print", prompt]
    elif client == "codex":
        instructions = state / "minimal-instructions.md"
        read_instructions = ("Use one read-only /usr/bin/cat command to read the requested qualification.txt file. "
                             "Do not run other commands, read other files, write files or use other tools. "
                             "Reply with the file contents only.\n")
        instructions.write_text(read_instructions)
        catalog = _codex_read_catalog(state, model, read_instructions)
        configs = {
            "model_instructions_file": str(instructions), "model_context_window": 8192,
            "model_catalog_json": str(catalog),
            "model_provider": "wrapper_active", "model_providers.wrapper_active.name": "Wrapper read qualification",
            "model_providers.wrapper_active.base_url": origin + "/v1",
            "model_providers.wrapper_active.wire_api": "responses",
            "model_providers.wrapper_active.env_key": "WRAPPER_API_KEY",
            "model_providers.wrapper_active.requires_openai_auth": False,
            "model_providers.wrapper_active.supports_websockets": False,
            "features.shell_tool": True, "features.unified_exec": False,
            "features.multi_agent": False, "agents.enabled": False,
            "features.hooks": False, "features.apps": False, "features.plugins": False,
            "mcp_servers": {}, "web_search": "disabled",
            "approval_policy": "never", "shell_environment_policy.inherit": "none",
            "shell_environment_policy.experimental_use_profile": False,
            "shell_environment_policy.ignore_default_excludes": False,
            "project_doc_max_bytes": 0,
        }
        command += ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
                    "--sandbox", "read-only", "--cd", str(state)]
        for key, value in configs.items():
            command += ["-c", key + "=" + json.dumps(value)]
        prompt = "Run exactly one read-only command: /usr/bin/cat -- qualification.txt. Then reply with the file contents only. Do not run any other command or modify any file."
        command += ["--model", model, "--json", prompt]
    elif client == "opencode":
        for key, suffix in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                            ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
            env[key] = str(state / ("xdg-" + suffix))
        env.update({"OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_CLAUDE_CODE": "true",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD": "true", "OPENCODE_AUTO_SHARE": "false"})
        # OpenCode 1.17.20 gives a fresh non-Git project the global worktree
        # "/", even with --dir. ReadTool asks using path.relative(worktree,
        # filePath); allow the exact unique fixture in absolute and root-
        # relative forms. A bare basename would allow a different root file.
        permissions = {"*": "deny", "read": {"*": "deny", str(fixture): "allow",
                                                os.path.relpath(fixture, "/"): "allow"}}
        agent = {"description": "Read the single qualification file without side effects.", "mode": "primary",
                 "prompt": "Use read on the exact requested file once and output its content only. Never use other tools.",
                 # Legacy tools normalization can append the wildcard deny
                 # after read, hiding the tool under last-match permissions.
                 # Keep one ordered permission source for this custom agent.
                 "permission": permissions}
        config = {"$schema": "https://opencode.ai/config.json", "enabled_providers": ["wrapper_active"],
                  "model": "wrapper_active/" + model, "small_model": "wrapper_active/" + model,
                  "permission": permissions, "mcp": {}, "plugin": [], "instructions": [],
                  "agent": {"qualification_reader": agent}, "provider": {"wrapper_active": {
                  "npm": "@ai-sdk/openai-compatible", "name": "Wrapper read qualification",
                  "options": {"baseURL": origin + "/v1", "apiKey": "{env:WRAPPER_API_KEY}"},
                  "models": {model: {"name": model, "limit": {"context": 8192, "output": 1024}}}}}}
        path = state / "opencode.json"
        path.write_text(json.dumps(config))
        env["OPENCODE_CONFIG"] = str(path)
        command += ["run", "--pure", "--dir", str(state), "--format", "json", "--agent", "qualification_reader",
                    "--model", "wrapper_active/" + model, prompt]
    else:
        raise ValueError("unsupported client")
    return command, env


def qualify_agent_read(client: str, origin: str, model: str, token: str, timeout: int) -> dict:
    check = {"kind": "agent_read_tool", "client": client, "model": model}
    try:
        with tempfile.TemporaryDirectory(prefix=f"wrapper-agent-read-{client}-") as directory:
            state = Path(directory)
            nonce = "qualification-" + secrets.token_hex(24)
            fixture = state / "qualification.txt"
            fixture.write_text(nonce + "\n")
            fixture.chmod(0o400)
            command, env = client_command(client, origin, model, token, state, fixture)
            # The nonce is confined to file contents. A prompt leak invalidates
            # the proof that the model learned it through an actual read tool.
            if any(nonce in argument for argument in command):
                raise ValueError("fixture value appeared in client arguments")
            check.update(parse_read_result(client, run_process(command, env, state, timeout), nonce, fixture))
            check["fixture_unchanged"] = fixture.read_text() == nonce + "\n"
            check["passed"] &= check["fixture_unchanged"]
    except Exception as error:
        check.update({"passed": False, "error_type": type(error).__name__})
    return check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", action="append", help="Lifecycle-active inference wrapper; default all eligible backends")
    parser.add_argument("--client", action="append", choices=tuple(CLI_PATHS), help="Actual client application; default all three")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    try:
        # Resolve the complete requested set before reading ANY environment.
        names = select_wrappers(args.wrapper, ROOT, include_registry=False)
        entries = load_wrappers(ROOT)
        for name in names:
            model = entries[name].get("client_qualification", {}).get("verified_model")
            port = entries[name].get("port")
            if not isinstance(model, str) or not model:
                raise WrapperScopeError(f"{name}: no explicitly qualified model in metadata")
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise WrapperScopeError(f"{name}: invalid service port")
    except WrapperScopeError as error:
        parser.error(str(error))
    clients = tuple(dict.fromkeys(args.client or CLI_PATHS))
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution": "one read-tool task per selected client and backend on live endpoints; no model substitution",
              "limits": "Claude/OpenCode output budget 1024. Codex context 8192 with minimal instructions; "
                        "its CLI does not guarantee a request output cap. Process timeout bounds each task.",
              "fixture": "Fresh random nonsecret nonce in a temporary read-only file; absent from prompts and reports",
              "source_fingerprints": source_fingerprints(names),
              "helper_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "client_versions": {client: installed_cli_version(client) for client in clients}, "checks": [],
              "references": ["https://learn.chatgpt.com/docs/cli/reference", "https://learn.chatgpt.com/docs/agent-approvals-security",
                             "https://opencode.ai/docs/permissions/", "https://code.claude.com/docs/en/permissions"]}
    from dotenv import dotenv_values
    for name in names:
        entry = entries[name]
        model = entry["client_qualification"]["verified_model"]
        env = dotenv_values(ROOT / name / ".env", interpolate=False)
        token = (env.get("BEARER_TOKEN") or "").strip()
        if not token or env.get("RESPONSES_UPSTREAM_MODE") == "chat":
            report["checks"].append({"wrapper": name, "kind": "preflight", "passed": False,
                                     "error_type": "MissingLocalBearerToken" if not token else "ForcedChatModeRejectsNativeQualification"})
            continue
        for client in clients:
            check = qualify_agent_read(client, f"http://127.0.0.1:{entry['port']}", model, token, args.timeout)
            check["wrapper"] = name
            report["checks"].append(check)
            print(f"{name} {client} read-tool task: {'PASS' if check['passed'] else 'FAIL'}", flush=True)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = sum(check["passed"] for check in report["checks"])
    report["failed"] = len(report["checks"]) - report["passed"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Agent read qualification: {report['passed']} passed / {report['failed']} failed", flush=True)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
