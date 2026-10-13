"""Exercise real Responses translators with isolated, credential-free upstreams.

The final response alone can hide broken intermediate events: agent clients
consume function argument deltas and item identities before completion. Load
the production translator ASTs without booting wrappers or loading .env files.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import random
import string
import sys
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace, ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.sse import IDLE, iter_chunks_with_idle, normalize_sse_newlines, close_stream

WRAPPERS = ("nvidia-python", "nous", "opencode", "blackbox", "openrouter", "shared-layer2")


def _chunk(delta=None, finish=None):
    return {"object": "chat.completion.chunk", "choices": [
        {"index": 0, "delta": delta or {}, "finish_reason": finish}]}


def _load(path, names, namespace):
    namespace.setdefault('close_stream', close_stream)
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if getattr(node, "name", None) in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)


def _frames(outputs):
    result = []
    for output in outputs:
        for line in output.splitlines():
            if line.startswith("data:"):
                value = line[5:].strip()
                result.append(value if value == "[DONE]" else json.loads(value))
    return result


class _Upstream:
    def __init__(self, wire):
        self.wire = wire
        self.closed = False

    async def _iterate(self):
        try:
            for data in self.wire:
                yield data
        finally:
            self.closed = True

    def __aiter__(self):
        return self._iterate()

    def release(self):
        self.closed = True


def _translate(wrapper, chunks, *, wire=None):
    async def run():
        upstream = _Upstream(wire if wire is not None else [
            ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            for chunk in chunks] + [b"data: [DONE]\n\n"])
        namespace = {
            "json": json, "time": time, "asyncio": asyncio,
            "random": random, "string": string,
            "logger": logging.getLogger("protocol-regressions"),
            "HEARTBEAT_MS": 100000,
            "_iter_chunks_with_idle": iter_chunks_with_idle, "_IDLE": IDLE,
            "_normalize_sse_newlines": normalize_sse_newlines,
            "pool": SimpleNamespace(release=lambda key: None),
            "_store_response": lambda *args: None,
            "_assistant_message_from_chat": lambda *args: {},
        }
        if wrapper == "shared-layer2":
            from common.compat import translate_openai_chat_sse_to_responses
            outputs = [output async for output in translate_openai_chat_sse_to_responses(
                upstream, "mock-model")]
        elif wrapper == "nous":
            _load(ROOT / wrapper / "src/main.py", {"ResponsesStreamState"}, namespace)
            state = namespace["ResponsesStreamState"]("resp-test", "mock-model")
            outputs = state.start()
            for chunk in chunks:
                outputs.extend(state.translate_chunk(chunk))
            outputs.extend(state.done())
            # The Nous state is terminated with [DONE] by stream_with_heartbeat.
            outputs.append("data: [DONE]\n\n")
        elif wrapper == "nvidia-python":
            path = ROOT / wrapper / "src/responses_compat.py"
            tree = ast.parse(path.read_text())
            names = {node.name for node in tree.body if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
            _load(path, names, namespace)
            namespace.update({"_extract_principal": lambda request: "test",
                              "_bounded_store": lambda *args: None,
                              "extract_internal_reasoning": lambda msg: msg})

            async def proxy(*args, **kwargs):
                return {"stream": upstream._iterate()}

            handler = namespace["ResponsesHandler"]({
                "pool": None, "resolve_target_model": lambda model: model,
                "proxy_openai": proxy, "forward_headers": lambda request: {},
            })
            _data, generator = await handler.translate_to_nim(
                None, {"model": "mock-model", "stream": True, "input": "hi"}, "mock-model")
            outputs = [output async for output in generator]
        elif wrapper == "openrouter":
            _load(ROOT / wrapper / "src/main.py", {
                "_translate_openai_stream_to_responses"}, namespace)
            generator = namespace["_translate_openai_stream_to_responses"](
                upstream, "mock-model")
            outputs = [output async for output in generator]
        elif wrapper == "blackbox":
            _load(ROOT / wrapper / "src/main.py", {
                "_responses_stream", "_emit_response_event"}, namespace)
            generator = namespace["_responses_stream"](
                upstream, "mock-key", "resp-test", "mock-model", {"messages": []}, "test")
            outputs = [output async for output in generator]
        else:
            # OpenCode's translator is an inner generator of its route. Lift
            # its real body while providing the route's closure explicitly.
            path = ROOT / wrapper / "src/main.py"
            route = next(node for node in ast.parse(path.read_text()).body
                         if isinstance(node, ast.AsyncFunctionDef) and node.name == "responses")
            candidates = [node for node in ast.walk(route)
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == "gen"
                          and "process_payload" in {getattr(child, "name", "")
                                                    for child in ast.walk(node)}]
            assert len(candidates) == 1
            generator_node = candidates[0]
            namespace.update({"rid": "resp-test", "model": "mock-model", "resp": upstream,
                              "key": "mock-key", "principal": "test", "chat_body": {"messages": []}})

            async def chunks_from_response(response):
                async for data in response:
                    yield False, data

            namespace["_chunk_stream"] = chunks_from_response
            exec(compile(ast.fix_missing_locations(ast.Module(
                body=[generator_node], type_ignores=[])), str(path), "exec"), namespace)
            outputs = [output async for output in namespace["gen"]()]
        return _frames(outputs)

    return asyncio.run(run())


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_reasoning_only_responses_close_every_item_and_complete_once(wrapper):
    frames = _translate(wrapper, [_chunk({"reasoning_content": "Thinking..."}),
                                  _chunk(finish="stop")])
    added = {frame["output_index"]: frame["item"] for frame in frames
             if isinstance(frame, dict) and frame["type"] == "response.output_item.added"}
    closed = {frame["output_index"]: frame["item"] for frame in frames
              if isinstance(frame, dict) and frame["type"] == "response.output_item.done"}
    terminals = [frame for frame in frames if isinstance(frame, dict)
                 and frame["type"] in ("response.completed", "response.failed")]
    assert len(terminals) == 1 and terminals[0]["type"] == "response.completed"
    assert added.keys() == closed.keys()
    assert frames[-1] == "[DONE]" and frames.count("[DONE]") == 1
    for index, item in added.items():
        assert item["id"] == closed[index]["id"]
        assert terminals[0]["response"]["output"][index] == closed[index]


@pytest.mark.parametrize("dialect", ("openai", "anthropic"))
@pytest.mark.parametrize("stream", (False, True))
def test_openrouter_previous_response_keeps_assistant_tool_call(dialect, stream):
    from starlette.responses import JSONResponse
    from common.sse import ClosingStreamingResponse as StreamingResponse
    from common.compat import (translate_anthropic_stream_to_openai_chat,
                               translate_openai_chat_sse_to_responses)
    from common.translations import anthropic_to_openai_response, openai_chat_to_anthropic_request

    async def run():
        saved = {}
        openai_reply = {"id": "upstream-turn", "choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "call_a",
                "type": "function", "function": {"name": "alpha", "arguments": '{"x":1}'}}]},
            "finish_reason": "tool_calls"}]}
        anthropic_reply = {"id": "upstream-turn", "type": "message", "role": "assistant",
                           "content": [{"type": "tool_use", "id": "call_a", "name": "alpha", "input": {"x": 1}}],
                           "stop_reason": "tool_use", "usage": {"input_tokens": 1, "output_tokens": 1}}

        async def proxy(*args, **kwargs):
            if not stream:
                return JSONResponse(openai_reply if dialect == "openai" else anthropic_reply)
            if dialect == "openai":
                wire = ["data: " + json.dumps(_chunk({"tool_calls": [{"index": 0, "id": "call_a",
                    "function": {"name": "alpha", "arguments": '{"x":1}'}}]})) + "\n\n",
                    "data: " + json.dumps(_chunk(finish="tool_calls")) + "\n\n", "data: [DONE]\n\n"]
            else:
                events = [
                    {"type": "message_start", "message": {"id": "upstream-turn", "usage": {"input_tokens": 1}}},
                    {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_a", "name": "alpha", "input": {}}},
                    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'}},
                    {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
                    {"type": "message_stop"},
                ]
                wire = ["event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events]
            return StreamingResponse(_Upstream(wire))

        namespace = {"json": json, "time": time, "asyncio": asyncio,
            "logger": logging.getLogger("protocol-regressions"), "HEARTBEAT_MS": 100000,
            "JSONResponse": JSONResponse, "StreamingResponse": StreamingResponse,
            "_check_free_only": lambda model: None,
            "_request_principal": lambda request: "tenant-a",
            "_is_anthropic_upstream": lambda: dialect == "anthropic",
            # This fixture exercises the explicitly selected Chat adapter;
            # native Responses continuation belongs to the upstream server.
            "_responses_use_native_upstream": lambda: False,
            "_proxy_request": proxy,
            "_store_response": lambda principal, rid, messages: saved.update({(principal, rid): messages}),
            "_get_stored_conversation": lambda principal, rid: list(saved.get((principal, rid), [])),
            "_iter_chunks_with_idle": iter_chunks_with_idle, "_IDLE": IDLE,
            "_normalize_sse_newlines": normalize_sse_newlines,
            "_translate_anthropic_stream_to_openai_chat": translate_anthropic_stream_to_openai_chat,
            "_translate_openai_chat_sse_to_responses": translate_openai_chat_sse_to_responses,
            "anthropic_to_openai_response": anthropic_to_openai_response,
            "openai_chat_to_anthropic_request": openai_chat_to_anthropic_request,
        }
        path = ROOT / "openrouter/src/main.py"
        names = {"responses", "responses_to_chat", "chat_to_responses", "_translate_openai_stream_to_responses"}
        optional = {"_remember_completed_response", "_remember_response_stream"}
        available = {getattr(node, "name", "") for node in ast.parse(path.read_text()).body}
        _load(path, names | (optional & available), namespace)

        async def request_json():
            return {"model": "mock-model", "input": "Use alpha", "stream": stream}

        response = await namespace["responses"](SimpleNamespace(json=request_json))
        if stream:
            frames = _frames([frame async for frame in response.body_iterator])
            rid = next(frame["response"]["id"] for frame in frames if isinstance(frame, dict)
                       and frame["type"] == "response.completed")
        else:
            rid = json.loads(response.body)["id"]
        followup = namespace["responses_to_chat"]({"model": "mock-model", "previous_response_id": rid,
            "input": [{"type": "function_call_output", "call_id": "call_a", "output": "ok"}]}, "tenant-a")
        assert [message["role"] for message in followup["messages"]] == ["user", "assistant", "tool"]
        tool_call = followup["messages"][1]["tool_calls"][0]
        assert tool_call["id"] == "call_a" and tool_call["function"]["name"] == "alpha"
        assert json.loads(tool_call["function"]["arguments"]) == {"x": 1}
        assert followup["messages"][2]["tool_call_id"] == "call_a"
        assert namespace["responses_to_chat"]({"model": "mock-model", "previous_response_id": rid,
            "input": "Other tenant"}, "tenant-b")["messages"] == [{"role": "user", "content": "Other tenant"}]

    asyncio.run(run())


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_parallel_responses_argument_deltas_are_valid_json_per_tool(wrapper):
    frames = _translate(wrapper, [
        _chunk({"tool_calls": [
            {"index": 0, "id": "call_a", "function": {"name": "alpha", "arguments": '{"x":'}},
            {"index": 1, "id": "call_b", "function": {"name": "beta", "arguments": '{"y":'}},
        ]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
        _chunk({"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}),
        _chunk(finish="tool_calls"),
    ])
    arguments = defaultdict(str)
    for frame in frames:
        if isinstance(frame, dict) and frame["type"] == "response.function_call_arguments.delta":
            arguments[frame["item_id"]] += frame["delta"]
    assert dict(arguments) == {"call_a": '{"x":1}', "call_b": '{"y":2}'}
    tools = [frame["item"] for frame in frames if isinstance(frame, dict)
             and frame["type"] == "response.output_item.done" and frame["item"]["type"] == "function_call"]
    assert {tool["call_id"]: json.loads(tool["arguments"]) for tool in tools} == {
        "call_a": {"x": 1}, "call_b": {"y": 2}}
    names = {frame["item"]["id"]: frame["item"]["name"] for frame in frames
             if isinstance(frame, dict) and frame["type"] == "response.output_item.added"
             and frame["item"]["type"] == "function_call"}
    assert names == {"call_a": "alpha", "call_b": "beta"}


@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_completed_outputs_keep_indices_when_tools_precede_reasoning(wrapper):
    frames = _translate(wrapper, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
            "name": "alpha", "arguments": "{}"}}]}),
        _chunk({"reasoning_content": "Checking the result."}),
        _chunk(finish="tool_calls"),
    ])
    closed = {frame["output_index"]: frame["item"] for frame in frames
              if isinstance(frame, dict) and frame["type"] == "response.output_item.done"}
    completed = next(frame["response"] for frame in frames if isinstance(frame, dict)
                     and frame["type"] == "response.completed")
    assert completed["output"] == [closed[index] for index in sorted(closed)]


@pytest.mark.parametrize("wrapper", ("nvidia-python", "opencode", "blackbox", "openrouter"))
def test_split_utf8_bytes_preserve_visible_model_text(wrapper):
    # These are legitimate content, including the exact characters the audit
    # proposed removing. Packet boundaries must not corrupt or filter text.
    text = "你好 ࠀ ｜ <unk> <s> café 👋"
    encoded = ("data: " + json.dumps(_chunk({"content": text}), ensure_ascii=False) + "\n\n").encode()
    frames = _translate(wrapper, [], wire=[bytes([value]) for value in encoded])
    received = "".join(frame["delta"] for frame in frames if isinstance(frame, dict)
                       and frame["type"] == "response.output_text.delta")
    assert received == text


@pytest.mark.parametrize("wrapper", ("nvidia-python", "opencode", "blackbox", "openrouter"))
@pytest.mark.parametrize("payload", [
    _chunk({"reasoning_content": "Reasoning tail"}),
    _chunk({"tool_calls": [{"index": 0, "id": "call_tail", "function": {
        "name": "alpha", "arguments": "{}"}}]}),
    {"error": {"message": "Failure at EOF"}},
], ids=("reasoning", "tool", "error"))
def test_partial_final_frame_keeps_protocol_semantics(wrapper, payload):
    frames = _translate(wrapper, [], wire=[("data: " + json.dumps(payload)).encode()])
    if "error" in payload:
        terminal = [frame for frame in frames if isinstance(frame, dict)
                    and frame["type"] in ("response.completed", "response.failed")]
        assert len(terminal) == 1 and terminal[0]["type"] == "response.failed"
    else:
        expected_type = "reasoning" if "reasoning_content" in payload["choices"][0]["delta"] else "function_call"
        completed = next(frame["response"] for frame in frames if isinstance(frame, dict)
                         and frame["type"] == "response.completed")
        assert expected_type in [item["type"] for item in completed["output"]]
    assert frames[-1] == "[DONE]" and frames.count("[DONE]") == 1


@pytest.mark.parametrize("base", ("https://mock.invalid", "https://mock.invalid/v1",
                                   "https://mock.invalid/prefix/v1/"))
def test_auto_discovery_normalizes_versioned_base_without_duplicate_v1(base):
    from common.compat import _probe, LAYER_ANTHROPIC

    class Reply:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            return Reply(404)

        def post(self, url, **kwargs):
            self.calls.append(url)
            return Reply(400 if url.endswith('/v1/messages') and '/v1/v1/' not in url else 404)

    session = Session()
    assert asyncio.run(_probe(session, base)) == LAYER_ANTHROPIC
    assert all('/v1/v1/' not in url for url in session.calls)


def _nvidia_store(max_bytes=180, max_entries=2):
    path = ROOT / "nvidia-python/src/responses_compat.py"
    namespace = {"json": json, "time": time,
                 "_RESPONSE_STORE": {}, "_RESPONSE_STORE_SIZES": {},
                 "_RESPONSE_STORE_TIMESTAMPS": {}, "_STORE_MAX_BYTES": max_bytes,
                 "_STORE_MAX_ENTRIES": max_entries, "_STORE_TTL_SEC": 60}
    requested = {"_store_key", "_bounded_store"}
    optional = {"_prune_response_store", "_get_stored_conversation"}
    available = {getattr(node, "name", "") for node in ast.parse(path.read_text()).body}
    _load(path, requested | (optional & available), namespace)
    # The original code read the underlying dict directly. Exercise that
    # behavior until the dedicated expiry/copy-aware read helper is added.
    get = namespace.get("_get_stored_conversation", lambda principal, rid:
                        namespace["_RESPONSE_STORE"].get(namespace["_store_key"](principal, rid)))
    return namespace, namespace["_bounded_store"], get


def test_nvidia_store_enforces_utf8_budget_and_rejects_single_oversized_entry():
    namespace, put, get = _nvidia_store(max_entries=20)
    messages = [{"role": "user", "content": "界" * 20}]
    put("tenant", "one", messages)
    put("tenant", "two", messages)
    assert get("tenant", "one") is None
    assert get("tenant", "two") == messages
    put("tenant", "huge", [{"role": "user", "content": "界" * 100}])
    assert get("tenant", "huge") is None
    assert sum(namespace["_RESPONSE_STORE_SIZES"].values()) <= 180
    assert sum(len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
               for messages in namespace["_RESPONSE_STORE"].values()) <= 180


def test_nvidia_store_ttl_prunes_on_read_and_replay_is_immutable(monkeypatch):
    namespace, put, get = _nvidia_store(max_bytes=10000)
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    messages = [{"role": "system", "content": "original instructions"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "call_a", "type": "function",
                    "function": {"name": "alpha", "arguments": "{}"}}]}]
    put("tenant-a", "id", messages)
    messages[0]["content"] = "mutated request"
    messages[1]["tool_calls"][0]["function"]["name"] = "mutated tool"
    assert get("tenant-b", "id") is None
    replay = get("tenant-a", "id")
    assert replay[0]["content"] == "original instructions"
    assert replay[1]["tool_calls"][0]["function"]["name"] == "alpha"
    replay[0]["content"] = "new turn instructions"
    replay[1]["tool_calls"].clear()
    assert get("tenant-a", "id")[0]["content"] == "original instructions"
    assert get("tenant-a", "id")[1]["tool_calls"]
    monkeypatch.setattr(time, "time", lambda: 1061.0)
    assert get("tenant-a", "id") is None
    assert namespace["_RESPONSE_STORE"] == namespace["_RESPONSE_STORE_SIZES"] == namespace["_RESPONSE_STORE_TIMESTAMPS"] == {}


def test_nvidia_store_respects_configured_entry_cap_and_replacement_order():
    namespace, put, get = _nvidia_store(max_bytes=10000)
    for rid in ("one", "two", "one", "three"):
        put("tenant", rid, [{"role": "user", "content": rid}])
    assert len(namespace["_RESPONSE_STORE"]) == 2
    assert get("tenant", "two") is None
    assert get("tenant", "one") == [{"role": "user", "content": "one"}]


def _nvidia_protocol_module(monkeypatch):
    namespace, put, get = _nvidia_store(max_bytes=10000, max_entries=20)
    namespace.update({"random": random, "string": string, "close_stream": close_stream,
                      "extract_internal_reasoning": lambda msg: {
                          "text": msg.get('content') or '', "reasoning": msg.get('reasoning_content') or ''}})
    path = ROOT / "nvidia-python/src/responses_compat.py"
    names = {node.name for node in ast.parse(path.read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    _load(path, names, namespace)
    namespace['_extract_principal'] = lambda request: 'tenant'
    module = ModuleType('_nvidia_regression.responses_compat')
    module.__dict__.update(namespace)
    package = ModuleType('_nvidia_regression')
    package.__path__ = []
    monkeypatch.setitem(sys.modules, '_nvidia_regression', package)
    monkeypatch.setitem(sys.modules, '_nvidia_regression.responses_compat', module)
    return namespace


def _anthropic_tool_wire():
    events = [
        {"type": "message_start", "message": {"id": "anthropic-turn", "usage": {"input_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_a", "name": "alpha", "input": {}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return ['event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events]


@pytest.mark.parametrize('stream', (False, True))
def test_nvidia_anthropic_upstream_preserves_input_and_tool_continuation(monkeypatch, stream):
    from starlette.responses import JSONResponse
    from common.sse import ClosingStreamingResponse as StreamingResponse
    from common.compat import (translate_anthropic_stream_to_openai_chat,
                               translate_openai_chat_sse_to_responses)
    from common.translations import anthropic_to_openai_response, openai_chat_to_anthropic_request

    async def run():
        storage = _nvidia_protocol_module(monkeypatch)
        requests = []
        reply = {'id': 'anthropic-turn', 'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'tool_use', 'id': 'call_a', 'name': 'alpha', 'input': {'x': 1}}],
                 'stop_reason': 'tool_use', 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        release_count = []

        async def proxy(body, request):
            requests.append(json.loads(json.dumps(body)))
            return 200, _Upstream(_anthropic_tool_wire()) if body.get('stream') else reply, SimpleNamespace(
                decrement_in_flight=lambda: release_count.append(True))

        namespace = {'json': json, 'os': SimpleNamespace(environ={}), '__package__': '_nvidia_regression',
                     'JSONResponse': JSONResponse, 'StreamingResponse': StreamingResponse,
                     '_translate_anthropic_stream_to_openai_chat': translate_anthropic_stream_to_openai_chat,
                     '_translate_openai_chat_sse_to_responses': translate_openai_chat_sse_to_responses,
                     'anthropic_to_openai_response': anthropic_to_openai_response,
                     'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request}
        path = ROOT / 'nvidia-python/src/main.py'
        method = next(node for node in ast.walk(ast.parse(path.read_text()))
                      if isinstance(node, ast.AsyncFunctionDef) and node.name == '_compat2_responses')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(path), 'exec'), namespace)
        server = SimpleNamespace(_compat2_proxy=proxy, _in_flight=1)
        result = await namespace['_compat2_responses'](server, {
            'model': 'mock-model', 'input': 'Use alpha', 'instructions': 'Follow instructions',
            'stream': stream, 'max_output_tokens': 64}, None)
        if stream:
            # Stop reading exactly when the successful terminal arrives.
            iterator = result.body_iterator
            async for frame in iterator:
                completed = [event['response'] for event in _frames([frame]) if isinstance(event, dict)
                             and event.get('type') == 'response.completed']
                if completed:
                    rid = completed[0]['id']
                    assert storage['_get_stored_conversation']('tenant', rid)
                    await iterator.aclose()
                    break
            assert release_count == [True]
        else:
            rid = json.loads(result.body)['id']
        assert requests[0]['system'] == 'Follow instructions'
        assert requests[0]['messages'][0]['content'] == 'Use alpha'
        assert requests[0]['max_tokens'] == 64
        await namespace['_compat2_responses'](server, {
            'model': 'mock-model', 'previous_response_id': rid, 'stream': False,
            'input': [{'type': 'function_call_output', 'call_id': 'call_a', 'output': 'ok'}]}, None)
        replay = requests[1]['messages']
        assert [message['role'] for message in replay] == ['user', 'assistant', 'user']
        assert replay[1]['content'][0]['type'] == 'tool_use'
        assert replay[1]['content'][0]['id'] == 'call_a'
        assert replay[2]['content'][0]['type'] == 'tool_result'
        assert replay[2]['content'][0]['tool_use_id'] == 'call_a'

    asyncio.run(run())


def test_nvidia_openai_stream_persists_before_completed_earlyclose(monkeypatch):
    async def run():
        namespace = _nvidia_protocol_module(monkeypatch)
        upstream = _Upstream([('data: ' + json.dumps(_chunk({'tool_calls': [
            {'index': 0, 'id': 'call_a', 'function': {'name': 'alpha', 'arguments': '{}'}}]})) + '\n\n').encode()])

        async def proxy(*args, **kwargs):
            return {'stream': upstream._iterate()}

        handler = namespace['ResponsesHandler']({'pool': None, 'resolve_target_model': lambda model: model,
            'proxy_openai': proxy, 'forward_headers': lambda request: {}})
        _, iterator = await handler.translate_to_nim(None, {
            'model': 'mock-model', 'input': 'Use alpha', 'stream': True}, 'mock-model')
        async for frame in iterator:
            completed = [event['response'] for event in _frames([frame]) if isinstance(event, dict)
                         and event.get('type') == 'response.completed']
            if completed:
                stored = namespace['_get_stored_conversation']('tenant', completed[0]['id'])
                assert stored[-1]['tool_calls'][0]['id'] == 'call_a'
                await iterator.aclose()
                break
        assert upstream.closed

    asyncio.run(run())


def test_nous_anthropic_stream_persists_complete_tool_turn_before_terminal():
    from common.compat import translate_openai_chat_sse_to_responses

    async def run():
        saved = {}

        async def store(principal, rid, messages):
            saved[(principal, rid)] = messages

        namespace = {'store_conversation': store}
        _load(ROOT / 'nous/src/main.py', {'_remember_compat_responses_stream'}, namespace)
        upstream = _Upstream([('data: ' + json.dumps(_chunk({'tool_calls': [
            {'index': 0, 'id': 'call_a', 'function': {'name': 'alpha', 'arguments': '{}'}}]})) + '\n\n').encode()])
        iterator = namespace['_remember_compat_responses_stream'](
            translate_openai_chat_sse_to_responses(upstream._iterate(), 'mock-model'),
            'tenant', [{'role': 'user', 'content': 'Use alpha'}])
        async for frame in iterator:
            completed = [event['response'] for event in _frames([frame]) if isinstance(event, dict)
                         and event.get('type') == 'response.completed']
            if completed:
                history = saved[('tenant', completed[0]['id'])]
                assert history[0] == {'role': 'user', 'content': 'Use alpha'}
                assert history[1]['tool_calls'][0]['id'] == 'call_a'
                await iterator.aclose()
                break
        assert upstream.closed

    asyncio.run(run())
