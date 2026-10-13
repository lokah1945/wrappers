#!/usr/bin/env python3
"""Explicit, bounded live qualification of metadata-approved production backends.

This helper sends real inference requests when executed. It does not start,
restart or configure services. Credentials stay in memory and child environment;
reports contain checks and source fingerprints, never response bodies or headers.
Passing a model smoke is not proof of every workload or future client version.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.wrapper_scope import WrapperScopeError, load_wrappers, select_wrappers
from common.model.identity import same_provider_model_id

CLI_PATHS = {
    "claude": Path("/root/.local/bin/claude"),
    "codex": Path("/root/.npm-global/bin/codex"),
    "opencode": Path("/root/.opencode/bin/opencode"),
}
TOOL_NAME = "qualification_echo"
SCHEMA = {"type": "object", "properties": {"value": {"type": "string", "enum": ["OK"]}},
          "required": ["value"], "additionalProperties": False}
PROMPT = 'Call qualification_echo exactly once with value "OK". After its result, reply exactly OK.'
CLI_SMOKE_PROMPT = "Reply with exactly two ASCII characters: uppercase O followed by uppercase K. No punctuation, quotes, markdown, explanation or other characters. Do not use tools."


def source_fingerprints(names: tuple[str, ...]) -> dict:
    paths = [ROOT / "wrappers.json", ROOT / "common/compat.py", ROOT / "common/body_guard.py"]
    paths.extend(ROOT / name / "src/main.py" for name in names)
    hashes = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in paths}
    combined = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    return {"files": hashes, "combined_sha256": combined,
            "meaning": "working-tree files at qualification time; service deployment requires a separate rollout record"}


def client_environment(token: str) -> dict[str, str]:
    # Keep the user's HOME and CODEX_HOME values unchanged. Do not inherit API
    # keys, OAuth, provider settings or proxy authentication from the parent.
    allowed = {"PATH", "HOME", "CODEX_HOME", "SHELL", "LANG", "LC_ALL", "TERM",
               "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["WRAPPER_API_KEY"] = token
    return env


def run_process(argv: list[str], env: dict[str, str], directory: Path, timeout: int) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=directory, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _stderr = process.communicate(timeout=timeout)
        return {"returncode": process.returncode, "stdout": stdout,
                "latency_sec": round(time.monotonic() - started, 3), "timed_out": False}
    except subprocess.TimeoutExpired:
        # Terminate only the process group this helper created, including child
        # CLI workers, before deleting its temporary configuration directory.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        return {"returncode": process.returncode, "stdout": "",
                "latency_sec": round(time.monotonic() - started, 3), "timed_out": True}


def installed_cli_version(name: str) -> dict:
    path = CLI_PATHS[name]
    if not path.is_file():
        return {"available": False, "version": None}
    try:
        with tempfile.TemporaryDirectory(prefix="wrapper-cli-version-") as directory:
            result = run_process([str(path), "--version"], client_environment(""), Path(directory), 15)
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?\b", result["stdout"])
        return {"available": result["returncode"] == 0, "version": match.group(0) if match else None}
    except Exception as error:
        return {"available": False, "version": None, "error_type": type(error).__name__}


def parse_cli_result(name: str, result: dict) -> dict:
    events = []
    try:
        single = json.loads(result["stdout"])
    except ValueError:
        single = None
    lines = [result["stdout"]] if isinstance(single, dict) else result["stdout"].splitlines()
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except ValueError:
            pass
    text = ""
    terminal = False
    tools = False
    if name == "claude":
        final = [event for event in events if event.get("type") == "result"]
        if final:
            event = final[-1]
            text = event.get("result", "")
            terminal = event.get("subtype") == "success" and not event.get("is_error", False)
    elif name == "codex":
        terminal = any(event.get("type") == "turn.completed" for event in events)
        for event in events:
            item = event.get("item", {})
            if isinstance(item, dict):
                if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                    text = item.get("text", "")
                tools |= item.get("type") in {"command_execution", "file_change", "mcp_tool_call",
                                               "web_search", "function_call"}
    else:
        parts = []
        for event in events:
            part = event.get("part", {})
            if not isinstance(part, dict):
                continue
            if event.get("type") == "text":
                parts.append(part.get("text", ""))
            terminal |= event.get("type") == "step_finish" and part.get("reason") == "stop"
            tools |= event.get("type") == "tool_use" or part.get("type") == "tool"
        text = "".join(piece for piece in parts if isinstance(piece, str))
    text = text if isinstance(text, str) else ""
    exact = text.strip() == "OK"
    return {"exit_code": result["returncode"], "timed_out": result["timed_out"],
            "terminal_event_observed": terminal, "has_visible_answer": bool(text.strip()),
            "answer_matches_OK": exact, "visible_characters": len(text),
            "tool_execution_observed": tools,
            "event_types": sorted({event.get("type") for event in events if isinstance(event.get("type"), str)}),
            "latency_sec": result["latency_sec"],
            "passed": result["returncode"] == 0 and terminal and exact and not tools}


def qualify_client(name: str, origin: str, model: str, token: str, timeout: int) -> dict:
    check = {"kind": "client", "client": name, "model": model}
    try:
        with tempfile.TemporaryDirectory(prefix=f"wrapper-live-{name}-") as directory:
            state = Path(directory)
            env = client_environment(token)
            command = [str(CLI_PATHS[name])]
            if name == "claude":
                env.update({"ANTHROPIC_BASE_URL": origin, "ANTHROPIC_API_KEY": token,
                            "ANTHROPIC_AUTH_TOKEN": token, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "256", "MAX_THINKING_TOKENS": "0"})
                command += ["--bare", "--safe-mode", "--setting-sources", "", "--strict-mcp-config",
                            "--tools", "", "--no-session-persistence", "--system-prompt",
                            CLI_SMOKE_PROMPT, "--model", model,
                            "--output-format", "json", "--print", CLI_SMOKE_PROMPT]
            elif name == "codex":
                instructions = state / "minimal-instructions.md"
                instructions.write_text(CLI_SMOKE_PROMPT + "\n")
                configs = {
                    "model_instructions_file": str(instructions), "model_context_window": 8192,
                    "model_provider": "wrapper_active", "model_providers.wrapper_active.name": "Wrapper aktif",
                    "model_providers.wrapper_active.base_url": origin + "/v1",
                    "model_providers.wrapper_active.wire_api": "responses",
                    "model_providers.wrapper_active.env_key": "WRAPPER_API_KEY",
                    "model_providers.wrapper_active.requires_openai_auth": False,
                    "model_providers.wrapper_active.supports_websockets": False,
                    "features.shell_tool": False, "features.unified_exec": False,
                    "features.multi_agent": False, "agents.enabled": False,
                    "features.hooks": False, "features.apps": False, "features.plugins": False,
                    "web_search": "disabled",
                }
                command += ["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                            "--skip-git-repo-check", "--sandbox", "read-only", "--cd", str(state)]
                for key, value in configs.items():
                    command += ["-c", key + "=" + json.dumps(value)]
                command += ["--model", model, "--json", CLI_SMOKE_PROMPT]
            else:
                for key, suffix in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                    ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")]:
                    env[key] = str(state / ("xdg-" + suffix))
                env.update({"OPENCODE_DISABLE_AUTOUPDATE": "true", "OPENCODE_DISABLE_MODELS_FETCH": "true"})
                config = {"$schema": "https://opencode.ai/config.json", "enabled_providers": ["wrapper_active"],
                    "model": "wrapper_active/" + model, "permission": "deny", "provider": {"wrapper_active": {
                    "npm": "@ai-sdk/openai-compatible", "name": "Wrapper aktif",
                    "options": {"baseURL": origin + "/v1", "apiKey": "{env:WRAPPER_API_KEY}"},
                    "models": {model: {"name": model, "limit": {"context": 8192, "output": 256}}}}}}
                (state / "opencode.json").write_text(json.dumps(config))
                command += ["run", "--pure", "--dir", str(state), "--format", "json", "--model",
                            "wrapper_active/" + model, CLI_SMOKE_PROMPT]
            check.update(parse_cli_result(name, run_process(command, env, state, timeout)))
    except Exception as error:
        check.update({"passed": False, "error_type": type(error).__name__})
    return check


def _visible(check: dict, text: str, terminal: str, expected: str) -> None:
    check.update({"terminal": terminal, "has_visible_answer": bool(text.strip()),
                  "answer_matches_OK": text.strip() == "OK", "visible_characters": len(text),
                  "passed": terminal == expected and text.strip() == "OK"})


def _arguments(value) -> bool:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    return value == {"value": "OK"}


def qualify_tool(surface: str, origin: str, model: str, token: str, timeout: int,
                 provider: str = "") -> dict:
    import httpx
    from openai import OpenAI
    from anthropic import Anthropic

    started = time.monotonic()
    check = {"kind": "tool_loop", "surface": surface, "model": model, "request_count": 0,
             "argument_schema": "required value:string enum[OK]; additionalProperties=false; strict=true",
             "history": "full explicit history; no claim of server-side credential-affinity continuation"}
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as transport:
            if surface == "messages":
                client = Anthropic(api_key=token, base_url=origin, max_retries=0, http_client=transport)
                history = [{"role": "user", "content": PROMPT}]
                tools = [{"name": TOOL_NAME, "description": "Return OK without side effects.",
                          "input_schema": SCHEMA, "strict": True}]
                check["request_count"] += 1
                first = client.messages.create(model=model, max_tokens=512, messages=history, tools=tools,
                                               tool_choice={"type": "tool", "name": TOOL_NAME})
                check["first_reported_model"] = first.model
                calls = [block for block in first.content if block.type == "tool_use"]
                valid = len(calls) == 1 and calls[0].name == TOOL_NAME and _arguments(calls[0].input)
                check.update({"tool_name_correct": len(calls) == 1 and calls[0].name == TOOL_NAME,
                              "arguments_valid": valid, "first_terminal": first.stop_reason})
                if not valid or first.stop_reason != "tool_use":
                    check["passed"] = False
                    return check
                history += [{"role": "assistant", "content": [block.model_dump(exclude_none=True) for block in first.content]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0].id, "content": "OK"}]}]
                check["request_count"] += 1
                final = client.messages.create(model=model, max_tokens=512, messages=history, tools=tools,
                                               tool_choice={"type": "none"})
                check["final_reported_model"] = final.model
                _visible(check, "".join(block.text for block in final.content if block.type == "text"),
                         final.stop_reason, "end_turn")
            else:
                client = OpenAI(api_key=token, base_url=origin + "/v1", max_retries=0, http_client=transport)
                if surface == "chat":
                    history = [{"role": "user", "content": PROMPT}]
                    tools = [{"type": "function", "function": {"name": TOOL_NAME,
                              "description": "Return OK without side effects.", "parameters": SCHEMA, "strict": True}}]
                    check["request_count"] += 1
                    first = client.chat.completions.create(model=model, max_tokens=512, messages=history, tools=tools,
                        tool_choice={"type": "function", "function": {"name": TOOL_NAME}})
                    check["first_reported_model"] = first.model
                    choice = first.choices[0]
                    calls = choice.message.tool_calls or []
                    valid = len(calls) == 1 and calls[0].function.name == TOOL_NAME and _arguments(calls[0].function.arguments)
                    check.update({"tool_name_correct": len(calls) == 1 and calls[0].function.name == TOOL_NAME,
                                  "arguments_valid": valid, "first_terminal": choice.finish_reason})
                    if not valid or choice.finish_reason != "tool_calls":
                        check["passed"] = False
                        return check
                    history += [choice.message.model_dump(exclude_none=True),
                                {"role": "tool", "tool_call_id": calls[0].id, "content": "OK"}]
                    check["request_count"] += 1
                    final = client.chat.completions.create(model=model, max_tokens=512, messages=history, tools=tools, tool_choice="none")
                    check["final_reported_model"] = final.model
                    _visible(check, final.choices[0].message.content or "", final.choices[0].finish_reason, "stop")
                else:
                    history = [{"role": "user", "content": PROMPT}]
                    tools = [{"type": "function", "name": TOOL_NAME, "description": "Return OK without side effects.",
                              "parameters": SCHEMA, "strict": True}]
                    def response_turn(input_items, tool_choice):
                        check["request_count"] += 1
                        with client.responses.stream(model=model, max_output_tokens=512, input=input_items,
                                                     tools=tools, tool_choice=tool_choice) as stream:
                            event_types = []
                            for event in stream:
                                event_types.append(event.type)
                            response = stream.get_final_response()
                        return response, event_types
                    first, first_events = response_turn(history, {"type": "function", "name": TOOL_NAME})
                    check["first_reported_model"] = first.model
                    calls = [item for item in first.output if item.type == "function_call"]
                    valid = len(calls) == 1 and calls[0].name == TOOL_NAME and _arguments(calls[0].arguments)
                    check.update({"tool_name_correct": len(calls) == 1 and calls[0].name == TOOL_NAME,
                                  "arguments_valid": valid, "first_terminal": first.status,
                                  "first_stream_events": len(first_events),
                                  "first_completed_events": first_events.count("response.completed")})
                    if not valid or first.status != "completed" or first_events.count("response.completed") != 1:
                        check["passed"] = False
                        return check
                    # Replay every native output item, including reasoning and
                    # opaque native fields, rather than translating its history.
                    history += [item.model_dump(exclude_none=True) for item in first.output]
                    history += [{"type": "function_call_output", "call_id": calls[0].call_id, "output": "OK"}]
                    final, final_events = response_turn(history, "none")
                    check["final_reported_model"] = final.model
                    check.update({"final_stream_events": len(final_events),
                                  "final_completed_events": final_events.count("response.completed")})
                    _visible(check, final.output_text, final.status, "completed")
                    check["passed"] &= final_events.count("response.completed") == 1
    except Exception as error:
        check.update({"passed": False, "error_type": type(error).__name__})
        status = getattr(error, "status_code", None)
        if isinstance(status, int):
            check["http_status"] = status
    finally:
        check["latency_sec"] = round(time.monotonic() - started, 3)
        reported = [check[key] for key in ("first_reported_model", "final_reported_model") if check.get(key)]
        check["reported_model_matches_request"] = all(
            same_provider_model_id(provider, model, value) for value in reported)
        check["reported_model_observed"] = bool(reported)
        check["passed"] = bool(check.get("passed")) and check["reported_model_matches_request"]
    return check


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", action="append", help="Active inference wrapper; default is all eligible backends")
    parser.add_argument("--clients", action="store_true", help="Run three actual client CLIs")
    parser.add_argument("--tools", action="store_true", help="Run two-turn real SDK tool loops on three surfaces")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("timeout must be positive")
    try:
        # Resolve every requested wrapper before reading ANY environment file.
        names = select_wrappers(args.wrapper, ROOT, include_registry=False)
        metadata = load_wrappers(ROOT)
        for name in names:
            model = metadata[name].get("client_qualification", {}).get("verified_model")
            if not isinstance(model, str) or not model:
                raise WrapperScopeError(f"{name}: metadata has no explicitly qualified model")
    except WrapperScopeError as error:
        parser.error(str(error))
    run_clients = args.clients or not args.tools
    run_tools = args.tools or not args.clients
    versions = {name: installed_cli_version(name) for name in CLI_PATHS} if run_clients else {}
    report = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
              "execution": "live production endpoints; real bounded inference; selected model never changes",
              "limits": "CLI 256 where configurable; SDK 512. Codex does not guarantee a request output cap. "
                        "Bounded smoke proves tested paths only. Full history does not qualify server-side continuation across key rotation.",
              "source_fingerprints": source_fingerprints(names), "client_versions": versions,
              "sdk_versions": {name: importlib.metadata.version(name) for name in ("openai", "anthropic")},
              "checks": []}
    from dotenv import dotenv_values
    for name in names:
        config = metadata[name]
        model = config["client_qualification"]["verified_model"]
        origin = f"http://127.0.0.1:{config['port']}"
        env = dotenv_values(ROOT / name / ".env", interpolate=False)
        token = env.get("BEARER_TOKEN")
        mode = env.get("RESPONSES_UPSTREAM_MODE") or "auto"
        if not token or mode == "chat":
            report["checks"].append({"wrapper": name, "model": model, "kind": "preflight", "passed": False,
                "error_type": "MissingLocalBearerToken" if not token else "ForcedChatModeRejectsNativeQualification"})
            continue
        report.setdefault("backend_policies", {})[name] = {"origin": origin, "model": model,
            "configured_responses_mode": mode, "policy": "native-first; deployment mode must also be checked against service environment"}
        if run_clients:
            for client in CLI_PATHS:
                check = qualify_client(client, origin, model, token, args.timeout)
                check["wrapper"] = name
                report["checks"].append(check)
                print(f"{name} {client}: {'PASS' if check['passed'] else 'FAIL'}", flush=True)
        if run_tools:
            for surface in ("chat", "responses", "messages"):
                provider = "nvidia" if name == "nvidia-python" else name
                check = qualify_tool(surface, origin, model, token, args.timeout, provider)
                check["wrapper"] = name
                report["checks"].append(check)
                print(f"{name} {surface} tool loop: {'PASS' if check['passed'] else 'FAIL'}", flush=True)
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["passed"] = sum(check["passed"] for check in report["checks"])
    report["failed"] = len(report["checks"]) - report["passed"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Live qualification: {report['passed']} passed / {report['failed']} failed", flush=True)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
