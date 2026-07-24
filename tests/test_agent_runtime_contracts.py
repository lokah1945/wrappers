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


def test_nous_retries_next_key_on_rate_limit_before_returning_error():
    wn = _load_nous()
    wn.KEY_POOL.keys = [wn.KeyEntry("key1", "bad-token"), wn.KeyEntry("key2", "good-token")]
    wn.KEY_POOL._rr = 0
    calls = []
    old_post = wn.post_nous
    old_auth = wn._read_token_from_auth_path

    async def fake_post(payload, token, stream=False, extra_headers=None):
        calls.append(token)
        if token == "bad-token":
            return 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    wn.post_nous = fake_post
    wn._read_token_from_auth_path = lambda: None
    try:
        status, result, key_entry = asyncio.run(wn.post_nous_with_retries({"model": "m"}, stream=False))
    finally:
        wn.post_nous = old_post
        wn._read_token_from_auth_path = old_auth

    assert status == 200
    assert calls == ["bad-token", "good-token"]
    assert key_entry is None
    assert wn.KEY_POOL.keys[0].is_blocked()
    assert wn.KEY_POOL.keys[1].in_flight == 0


def test_opencode_key_pool_rotates_and_skips_cooled_down_key():
    oc = _load_opencode()
    kp = oc.KeyPool()
    old_env = {k: os.environ.get(k) for k in ("OPENCODE_API_KEY_1", "OPENCODE_API_KEY_2")}
    os.environ["OPENCODE_API_KEY_1"] = "sk-test-key-111111"
    os.environ["OPENCODE_API_KEY_2"] = "sk-test-key-222222"
    try:
        kp.load_from_env()
        first = asyncio.run(kp.acquire())["key"]
        second = asyncio.run(kp.acquire())["key"]
        assert first.label != second.label
        kp.release(first)
        kp.release(second)
        kp.mark_failure(first, 429, retry_after=60)
        third = asyncio.run(kp.acquire())["key"]
        assert third.label == second.label
        kp.release(third)
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_nvidia_key_pool_inflight_reservation_releases_once():
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "opencode" not in p]
    sys.path.insert(0, str(ROOT / "nvidia-python"))
    from src.key_pool import KeyPool  # type: ignore

    old = os.environ.get("NVIDIA_API_KEY_1")
    os.environ["NVIDIA_API_KEY_1"] = "nvapi-test-key-111111"
    try:
        kp = KeyPool().load_from_env()
        entry = asyncio.run(kp.acquire("nvidia/test"))["key"]
        assert entry.in_flight == 1
        kp.release_success(entry)
        assert entry.in_flight == 0
    finally:
        if old is None:
            os.environ.pop("NVIDIA_API_KEY_1", None)
        else:
            os.environ["NVIDIA_API_KEY_1"] = old


def test_opencode_proxy_retries_next_key_before_returning_upstream_error():
    oc = _load_opencode()
    old_pool = oc.pool
    old_proxy = oc.proxy_request
    old_env = {k: os.environ.get(k) for k in ("OPENCODE_API_KEY_1", "OPENCODE_API_KEY_2")}
    os.environ["OPENCODE_API_KEY_1"] = "sk-test-key-bad111"
    os.environ["OPENCODE_API_KEY_2"] = "sk-test-key-good222"
    calls = []

    async def fake_proxy(method, url, json_body=None, headers=None, is_stream=False):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        calls.append(token)
        if token == "sk-test-key-bad111":
            return 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    class Req:
        headers = {}

    try:
        oc.pool = oc.KeyPool().load_from_env()
        oc.proxy_request = fake_proxy
        status, data, key = asyncio.run(oc.proxy_request_with_pool("POST", "https://example.test/chat/completions", {"model": "m"}, Req()))
    finally:
        oc.pool = old_pool
        oc.proxy_request = old_proxy
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert status == 200
    assert key is None
    assert calls == ["sk-test-key-bad111", "sk-test-key-good222"]
    assert data["choices"][0]["message"]["content"] == "ok"


def _load_blackbox():
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "opencode" not in p and "nvidia-python" not in p]
    sys.path.insert(0, str(ROOT / "blackbox"))
    import src.main as bb  # type: ignore

    return bb


def test_blackbox_free_only_defaults_true_and_blocks_non_free_model():
    bb = _load_blackbox()
    old = os.environ.pop("FREE_ONLY", None)
    try:
        assert bb.free_only_enabled() is True
        assert bb.model_allowed("blackboxai/nvidia/nemotron-3-super-120b-a12b:free")
        assert not bb.model_allowed("blackboxai/openai/gpt-5.5")
    finally:
        if old is not None:
            os.environ["FREE_ONLY"] = old


def test_blackbox_responses_to_chat_repairs_orphan_tool_result():
    bb = _load_blackbox()
    out = bb.responses_to_chat({
        "model": "blackboxai/nvidia/nemotron-3-super-120b-a12b:free",
        "input": [{"type": "function_call_output", "call_id": "missing", "output": "ok"}],
    })
    assert not any(m.get("role") == "tool" for m in out["messages"])
    assert out["messages"][-1]["role"] == "user"


def test_blackbox_proxy_retries_next_key_before_error():
    bb = _load_blackbox()
    old_pool = bb.pool
    old_proxy = bb.proxy_request
    old_env = {k: os.environ.get(k) for k in ("BLACKBOX_API_KEY_1", "BLACKBOX_API_KEY_2")}
    os.environ["BLACKBOX_API_KEY_1"] = "sk-blackbox-bad111"
    os.environ["BLACKBOX_API_KEY_2"] = "sk-blackbox-good222"
    calls = []

    async def fake_proxy(method, url, json_body=None, headers=None, is_stream=False):
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        calls.append(token)
        if token == "sk-blackbox-bad111":
            return 429, {"error": {"message": "rate limited", "type": "rate_limit_error"}}
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    class Req:
        headers = {}

    try:
        bb.pool = bb.KeyPool().load_from_env()
        bb.proxy_request = fake_proxy
        status, data, key = asyncio.run(bb.proxy_request_with_pool("POST", "https://example.test/chat/completions", {"model": "m"}, Req()))
    finally:
        bb.pool = old_pool
        bb.proxy_request = old_proxy
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    assert status == 200
    assert key is None
    assert calls == ["sk-blackbox-bad111", "sk-blackbox-good222"]
    assert data["choices"][0]["message"]["content"] == "ok"


def test_blackbox_anthropic_tools_structured_no_dsml():
    bb = _load_blackbox()
    body = {
        "model": "blackboxai/nvidia/nemotron-3-super-120b-a12b:free",
        "max_tokens": 32,
        "messages": [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "plan"}, {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pwd"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        ],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }
    oai = bb.anthropic_to_openai(body)
    blob = json.dumps(oai, ensure_ascii=False)
    assert "DSML" not in blob.replace("\uff5c", "|")
    assert any(m.get("tool_calls") for m in oai["messages"])
    assert any(m.get("role") == "tool" for m in oai["messages"])
    a = bb.openai_to_anthropic("m", {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": "Bash", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}], "usage": {}})
    assert a["stop_reason"] == "tool_use"
    assert any(c["type"] == "tool_use" for c in a["content"])


def _load_nvidia_main():
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "opencode" not in p and "blackbox" not in p]
    sys.path.insert(0, str(ROOT / "nvidia-python"))
    import src.main as nv  # type: ignore

    return nv


def test_nvidia_transient_unavailable_probe_does_not_block_explicit_model():
    nv = _load_nvidia_main()
    nv._unavailable_models.add("moonshotai/kimi-k2.6")
    try:
        assert nv.is_model_unavailable("moonshotai/kimi-k2.6") is False
        nv._retired_models.add("moonshotai/kimi-k2.6")
        assert nv.is_model_unavailable("moonshotai/kimi-k2.6") is True
    finally:
        nv._unavailable_models.discard("moonshotai/kimi-k2.6")
        nv._retired_models.discard("moonshotai/kimi-k2.6")


def test_nvidia_kimi_clamps_max_tokens_to_build_nvidia_cap():
    nv = _load_nvidia_main()
    body = {"model": "moonshotai/kimi-k2.6", "max_tokens": 200000, "max_completion_tokens": 200000}
    nv.clamp_max_tokens_for_model(body, "moonshotai/kimi-k2.6")
    assert body["max_tokens"] == 16384
    assert body["max_completion_tokens"] == 16384


def test_nvidia_kimi_skips_reasoning_effort_injection_for_claude_code_thinking():
    nv = _load_nvidia_main()
    body = {"model": "moonshotai/kimi-k2.6", "messages": []}
    nv.translate_thinking_to_nim(body, "moonshotai/kimi-k2.6", {"type": "enabled", "budget_tokens": 1024})
    assert "reasoning_effort" not in body
    assert "chat_template_kwargs" not in body
