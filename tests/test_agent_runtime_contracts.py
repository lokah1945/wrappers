#!/usr/bin/env python3
"""Deep runtime contracts for agent clients.

These tests target the failure mode that hurts Claude Code/Codex/Hermes most:
streams or tool loops that do not close deterministically.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("LOG_FILE", "/tmp/wrapper-audit-logs/runtime-contracts.log")
os.environ.setdefault("EVENTS_FILE", "/tmp/wrapper-audit-logs/e.jsonl")
os.environ.setdefault("METRICS_DB", "/tmp/wrapper-audit-logs/m.db")
os.environ.setdefault("VERIFY_ON_BOOT", "false")
os.environ.setdefault("BEARER_TOKEN", "")


def _load_nvidia_responses():
    sys.path.insert(0, str(ROOT / "nvidia-python"))
    from src.responses_compat import ResponsesHandler, _RESPONSE_STORE  # type: ignore

    return ResponsesHandler, _RESPONSE_STORE


def _load_nous():
    path = ROOT / "nous" / "wrapper_nous.py"
    spec = importlib.util.spec_from_file_location("wrapper_nous_runtime_contracts", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_opencode():
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "nvidia-python" not in p]
    sys.path.insert(0, str(ROOT / "opencode"))
    import src.main as oc  # type: ignore

    return oc


def _sse_types(blob: str) -> list[str]:
    out = []
    for block in blob.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: ") and line[6:].startswith("{"):
                out.append(json.loads(line[6:]).get("type"))
    return out


def test_nvidia_responses_stream_eof_completed_before_done_and_no_late_delta():
    ResponsesHandler, _STORE = _load_nvidia_responses()
    _STORE.clear()

    async def fake_proxy(chat_body, headers, model, request):
        async def upstream():
            yield ("data: " + json.dumps({"choices": [{"delta": {"content": "hello"}}]}) + "\n\n").encode()
            yield (
                "data: "
                + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
                + "\n\n"
            ).encode()
            # Intentionally no data: [DONE]

        return {"stream": upstream(), "status": 200}

    handler = ResponsesHandler({
        "pool": type("Pool", (), {"models_cached": []})(),
        "resolve_target_model": lambda m: m,
        "proxy_openai": fake_proxy,
        "forward_headers": lambda request: {},
        "CURATED_GENAI": [],
    })

    async def collect():
        result, stream = await handler.translate_to_nim(None, {"model": "nvidia/test", "input": "hi", "stream": True}, "nvidia/test")
        assert result is None
        chunks = []
        async for ev in stream:
            chunks.append(ev)
        return "".join(chunks)

    blob = asyncio.run(collect())
    types = _sse_types(blob)
    assert "response.completed" in types
    assert blob.rstrip().endswith("data: [DONE]")
    completed_idx = types.index("response.completed")
    assert "response.output_text.delta" not in types[completed_idx + 1 :]
    assert _STORE, "streaming Responses must store conversation for previous_response_id"


def test_nvidia_responses_nonstream_stores_assistant_tool_calls_for_next_turn():
    ResponsesHandler, _STORE = _load_nvidia_responses()
    _STORE.clear()
    captured = []

    async def fake_proxy(chat_body, headers, model, request):
        captured.append(chat_body)
        return {
            "status": 200,
            "data": {
                "id": "chatcmpl_tool",
                "choices": [{
                    "message": {"content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]},
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    handler = ResponsesHandler({
        "pool": type("Pool", (), {"models_cached": []})(),
        "resolve_target_model": lambda m: m,
        "proxy_openai": fake_proxy,
        "forward_headers": lambda request: {},
        "CURATED_GENAI": [],
    })

    async def run():
        resp, _ = await handler.translate_to_nim(None, {"model": "nvidia/test", "input": "use tool"}, "nvidia/test")
        return resp

    resp = asyncio.run(run())
    stored = _STORE[resp["id"]]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in stored)
    assert stored[-1]["tool_calls"][0]["id"] == "call_1"


def test_nous_zero_chunk_anthropic_stream_still_message_stops():
    wn = _load_nous()
    st = wn.AnthropicStreamState("m")
    ev = st.done()
    types = [e["type"] for e in ev]
    assert types == ["message_start", "message_delta", "message_stop"]
    assert ev[-2]["data"]["usage"]["input_tokens"] == 0


def test_opencode_orphan_tool_output_repaired_and_console_script_exists():
    oc = _load_opencode()
    assert hasattr(oc, "main")
    out = oc.responses_to_chat({
        "model": "deepseek-v4-flash-free",
        "input": [{"type": "function_call_output", "call_id": "missing_call", "output": "ok"}],
    })
    assert not any(m.get("role") == "tool" for m in out["messages"])
    assert out["messages"][-1]["role"] == "user"
    assert "Tool result" in out["messages"][-1]["content"]
