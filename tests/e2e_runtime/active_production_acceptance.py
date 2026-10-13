#!/usr/bin/env python3
"""Production contracts over real sockets and official SDKs, active wrappers only.

No operator credentials, .env files, production ports or model state are used.
The controlled upstream records requests to prove model identity, opaque native
fields, key rotation, tool continuation and disconnect cleanup end to end.
This verifies the installed SDK/API contracts; it does not promise compatibility
with every future client or upstream feature.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
from aiohttp import web
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from process_isolation import assert_ports_free, isolated_environment, stop_process

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common.wrapper_scope import require_production, select_wrappers
WRAPPERS = {
    "nvidia-python": (19401, "NVIDIA", False),
    "nous": (19402, "NOUS", False),
    "openrouter": (19406, "OPENROUTER", True),
}
TOKEN = "acceptance-client-token"
WRAPPERS = {name: config for name, config in WRAPPERS.items()
            if name in select_wrappers(repo=ROOT, include_registry=False)}
UPSTREAM_PORT = 19499
OPAQUE = {"nested": [None, False, {"future": "界 👋"}], "revision": 500}
TOOLS = {"call_a": ("alpha", {"x": 1}), "call_b": ("beta", {"y": 2})}
NOUS_REQUIRED_TAGS = ["product=wrapper-nous", "client=wrapper-nous", "user=wrapper-nous"]


class Upstream:
    def __init__(self):
        self.records = []
        self.attempted_keys = defaultdict(set)
        self.cancelled = set()
        self.catalog_requests = 0

    async def models(self, request):
        self.catalog_requests += 1
        return web.json_response({"object": "list", "data": [
            {"id": "mock/normal", "object": "model", "owned_by": "acceptance",
             "context_length": 1048576, "max_output_tokens": 8192,
             "future_model_metadata": {"preserved": True}},
            {"id": "mock/unknown-context", "object": "model"}]})

    def chat_reply(self, body):
        model = body["model"]
        message = {"role": "assistant", "content": "Acceptance answer"}
        if "tools" in model:
            message = {"role": "assistant", "content": None,
                       "reasoning_content": "Checking both tools",
                       "tool_calls": [{"id": call_id, "type": "function", "function": {
                           "name": name, "arguments": json.dumps(arguments)}}
                           for call_id, (name, arguments) in TOOLS.items()]}
        return {"id": "chatcmpl-acceptance", "object": "chat.completion",
                "created": 1760000000, "model": model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if "tools" in model else "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
                "opaque_future_response": OPAQUE}

    def anthropic_reply(self, body):
        content = [{"type": "text", "text": "Acceptance answer"}]
        if "tools" in body["model"]:
            content = [{"type": "thinking", "thinking": "Checking both tools", "signature": "test-signature"}]
            content += [{"type": "tool_use", "id": call_id, "name": name, "input": arguments}
                        for call_id, (name, arguments) in TOOLS.items()]
        return {"id": "msg_acceptance", "type": "message", "role": "assistant",
                "model": body["model"], "content": content,
                "stop_reason": "tool_use" if "tools" in body["model"] else "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 8, "output_tokens": 4},
                "opaque_future_response": OPAQUE}

    def chat_frames(self, body):
        def chunk(delta, finish=None):
            return {"id": "chatcmpl-acceptance", "object": "chat.completion.chunk",
                    "created": 1760000000, "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "opaque_future_response": OPAQUE}
        if "tools" in body["model"]:
            frames = [chunk({"role": "assistant", "reasoning_content": "Checking both tools"}),
                      chunk({"tool_calls": [
                          {"index": 0, "id": "call_a", "type": "function", "function": {"name": "alpha", "arguments": '{"x":'}},
                          {"index": 1, "id": "call_b", "type": "function", "function": {"name": "beta", "arguments": '{"y":'}}]}),
                      chunk({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
                      chunk({"tool_calls": [{"index": 1, "function": {"arguments": "2}"}}]}),
                      chunk({}, "tool_calls")]
        else:
            frames = [chunk({"role": "assistant", "content": "Acceptance answer"}), chunk({}, "stop")]
        return ["data: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames] + ["data: [DONE]\n\n"]

    def anthropic_frames(self, body):
        reply = self.anthropic_reply(body)
        frames = [{"type": "message_start", "message": {**reply, "content": [], "stop_reason": None}}]
        for index, block in enumerate(reply["content"]):
            kind = block["type"]
            start = dict(block)
            if kind == "thinking":
                start["thinking"] = ""
                delta = {"type": "thinking_delta", "thinking": block["thinking"]}
            elif kind == "tool_use":
                start["input"] = {}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                start["text"] = ""
                delta = {"type": "text_delta", "text": block["text"]}
            frames += [{"type": "content_block_start", "index": index, "content_block": start},
                       {"type": "content_block_delta", "index": index, "delta": delta},
                       {"type": "content_block_stop", "index": index}]
        frames += [{"type": "message_delta", "delta": {"stop_reason": reply["stop_reason"], "stop_sequence": None},
                    "usage": {"output_tokens": 4}}, {"type": "message_stop"}]
        return ["event: " + frame["type"] + "\ndata: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames]

    def responses_reply(self, body):
        output = [{"id": "msg_native", "type": "message", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "Acceptance answer", "annotations": []}],
                   "opaque_future_output": OPAQUE}]
        if "tools" in body["model"]:
            output = [{"id": "rs_native", "type": "reasoning", "summary": [
                {"type": "summary_text", "text": "Checking both tools"}]}]
            output += [{"id": "fc_native_" + call_id, "type": "function_call", "status": "completed",
                        "call_id": call_id, "name": name, "arguments": json.dumps(arguments)}
                       for call_id, (name, arguments) in TOOLS.items()]
        return {"id": "resp_native_acceptance", "object": "response", "created_at": 1760000000,
                "status": "completed", "error": None, "incomplete_details": None,
                "model": body["model"], "output": output, "parallel_tool_calls": True,
                "tools": body.get("tools", []), "tool_choice": "auto", "temperature": 1.0, "top_p": 1.0,
                "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 1}},
                "opaque_future_response": OPAQUE}

    def responses_frames(self, body):
        reply = self.responses_reply(body)
        initial = {**reply, "status": "in_progress", "output": []}
        frames = [{"type": "response.created", "response": initial},
                  {"type": "response.in_progress", "response": initial},
                  {"type": "response.future_extension", "opaque": OPAQUE}]
        for index, item in enumerate(reply["output"]):
            start = {**item, "status": "in_progress"}
            if item["type"] == "message":
                start["content"] = []
            elif item["type"] == "function_call":
                start["arguments"] = ""
            elif item["type"] == "reasoning":
                start["summary"] = []
            frames.append({"type": "response.output_item.added", "output_index": index, "item": start})
            common = {"item_id": item["id"], "output_index": index}
            if item["type"] == "message":
                part = item["content"][0]
                frames += [{"type": "response.content_part.added", **common, "content_index": 0,
                            "part": {**part, "text": ""}},
                           {"type": "response.output_text.delta", **common, "content_index": 0, "delta": part["text"]},
                           {"type": "response.output_text.done", **common, "content_index": 0, "text": part["text"]},
                           {"type": "response.content_part.done", **common, "content_index": 0, "part": part}]
            elif item["type"] == "function_call":
                frames += [{"type": "response.function_call_arguments.delta", **common, "delta": item["arguments"]},
                           {"type": "response.function_call_arguments.done", **common, "name": item["name"],
                            "arguments": item["arguments"]}]
            frames.append({"type": "response.output_item.done", "output_index": index, "item": item})
        frames.append({"type": "response.completed", "response": reply})
        for sequence, frame in enumerate(frames):
            frame["sequence_number"] = sequence
        return ["event: " + frame["type"] + "\ndata: " + json.dumps(frame, ensure_ascii=False) + "\n\n" for frame in frames]

    async def inference(self, request):
        body = await request.json()
        key = request.headers.get("Authorization", "").removeprefix("Bearer ") or request.headers.get("x-api-key", "")
        self.records.append({"path": request.path, "body": body, "key": key,
                             "headers": {k.lower(): v for k, v in request.headers.items()}})
        model = body["model"]
        if '/context-' in model:
            fields = [key for key in ('max_tokens', 'max_completion_tokens', 'max_output_tokens') if key in body]
            if model.endswith('/context-input-overflow'):
                return web.json_response({'error': {'code': 'context_length_exceeded',
                    'message': 'prompt is too long: 12000 tokens > 10000 maximum',
                    'opaque': OPAQUE}}, status=400)
            if fields and body[fields[0]] > 2000:
                return web.json_response({'error': {'code': 'context_length_exceeded',
                    'message': "This model's maximum context length is 10000 tokens. "
                               'You requested 12000 tokens (8000 in the messages, 4000 in the completion).',
                    'opaque': OPAQUE}}, status=400)
            if model.endswith('/context-rotation'):
                records = [r for r in self.records if r['body']['model'] == model]
                if key == records[0]['key']:
                    return web.json_response({'error': {'type': 'rate_limit_error',
                        'message': 'Controlled credential quota'}}, status=429, headers={'Retry-After': '1'})
        if "/native-unavailable" in model and request.path.endswith('/responses'):
            suffix = model.rsplit('-', 1)[-1]
            status = int(suffix) if suffix in ('404', '405', '501') else 404
            return web.json_response({"error": {"type": "invalid_request_error", "code": "route_not_found",
                                                 "message": "Native Responses endpoint unavailable"}}, status=status)
        if "/retry" in model:
            status = int(model.rsplit("-", 1)[1])
            tried = self.attempted_keys[model]
            tried.add(key)
            # Only the fourth distinct credential works; repeated use of a
            # failed key cannot make this test pass.
            if len(tried) < 4:
                kind = "rate_limit_error" if status == 429 else "authentication_error" if status == 401 else "insufficient_quota"
                return web.json_response({"error": {"message": "Credential usage limit" if status == 402 else "Credential rejected",
                                                      "type": kind}}, status=status, headers={"Retry-After": "1"})
        if "/exhaust" in model:
            return web.json_response({"error": {"message": "Credential rate limited", "type": "rate_limit_error"}},
                                     status=429, headers={"Retry-After": "1"})
        anthro = request.path.endswith("/messages")
        responses = request.path.endswith("/responses")
        if not body.get("stream"):
            return web.json_response(self.responses_reply(body) if responses else self.anthropic_reply(body) if anthro else self.chat_reply(body))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Connection": "close"})
        response.force_close()
        await response.prepare(request)
        frames = self.responses_frames(body) if responses else self.anthropic_frames(body) if anthro else self.chat_frames(body)
        try:
            if "/cancel" in model:
                # Keep generating until the consumer closes its socket.
                await response.write(frames[0].encode())
                for _ in range(150):
                    await asyncio.sleep(0.05)
                    await response.write(b": upstream still alive\n\n")
            else:
                for frame in frames:
                    await response.write(frame.encode())
            await response.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            self.cancelled.add(model)
        return response


async def wait_health(client, port, process):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"wrapper exited before readiness ({process.returncode})")
        try:
            async with client.get(f"http://127.0.0.1:{port}/health", timeout=aiohttp.ClientTimeout(total=2)) as reply:
                if reply.status == 200:
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(0.2)
    raise AssertionError("wrapper health did not become available")


def start_wrapper(name, layer, logs, response_mode="chat"):
    require_production(name, ROOT)
    port, prefix, versioned = WRAPPERS[name]
    env = isolated_environment()
    env.update({"BEARER_TOKEN": TOKEN, "LISTEN_PORT": str(port),
                "LISTEN_HOST": "127.0.0.1", "COMPATIBILITY_LAYER": str(layer),
                "RATE_LIMIT_RPM": "0", "SOFT_LIMIT_RPM": "100000", "HARD_LIMIT_RPM": "100000",
                "NOUS_HARD_LIMIT_RPM": "100000", "HEARTBEAT_INTERVAL_MS": "100",
                "FREE_ONLY": "no", "RETRY_BACKOFF_BASE_SEC": "0", "PACING_ENABLED": "false",
                "RATE_LIMIT_COOLDOWN_SEC": "1", "AUTH_KEY_COOLDOWN_SEC": "1",
                "TRANSIENT_KEY_COOLDOWN_SEC": "1", "KEY_COOLDOWN_MAX_SEC": "1", "VERIFY_ON_BOOT": "false",
                "PYTHONPATH": f"{ROOT}:{ROOT / name}",
                f"{prefix}_BASE_URL": f"http://127.0.0.1:{UPSTREAM_PORT}" + ("/v1" if versioned else "")})
    for index in range(1, 5):
        env[f"{prefix}_API_KEY_{index}"] = f"mock-acceptance-key-{index:010d}"
    if response_mode is not None:
        env["RESPONSES_UPSTREAM_MODE"] = response_mode
    log = (logs / f"{name}-layer{layer}-responses-{response_mode or 'default'}.log").open("w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "src.main:app", "--host", "127.0.0.1",
                                "--port", str(port), "--log-level", "warning"],
                               cwd=ROOT / name, env=env, stdout=log, stderr=subprocess.STDOUT)
    return process, log


class Acceptance:
    def __init__(self, upstream, client):
        self.upstream, self.client = upstream, client
        self.checks = []

    async def check(self, label, action):
        try:
            await action()
        except Exception as exc:
            self.checks.append({"check": label, "passed": False, "error": f"{type(exc).__name__}: {exc}"[:1600]})
            print(f"FAIL {label}: {exc}", flush=True)
        else:
            self.checks.append({"check": label, "passed": True})
            print(f"PASS {label}", flush=True)

    async def post(self, port, path, body, headers=None):
        async with self.client.post(f"http://127.0.0.1:{port}{path}", json=body,
                                    headers=headers or {"Authorization": f"Bearer {TOKEN}"},
                                    timeout=aiohttp.ClientTimeout(total=20)) as reply:
            return reply.status, await reply.text()

    async def context_recovery(self, name, path, stream, *, rotation=False, oversized=False):
        ending = 'context-input-overflow' if oversized else 'context-rotation' if rotation else 'context-recovery'
        model = f'mock/{name}-{path.rsplit("/", 1)[-1]}-{int(stream)}/{ending}'
        body = {'model': model, 'stream': stream, 'opaque_future_request': OPAQUE}
        field = 'max_output_tokens' if path == '/v1/responses' else 'max_tokens'
        body[field] = 4000
        if path == '/v1/responses':
            body.update(input=[{'type': 'function_call_output', 'call_id': 'native-call', 'output': 'preserve'}],
                        previous_response_id='native-history-retained')
        elif path == '/v1/messages':
            body['messages'] = [{'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'call', 'name': 'run', 'input': {}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': 'preserve'}]}]
        else:
            body['messages'] = [{'role': 'assistant', 'tool_calls': [{'id': 'call', 'type': 'function',
                'function': {'name': 'run', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'call', 'content': 'preserve'}]
        before = len(self.upstream.records)
        async with self.client.post(f'http://127.0.0.1:{WRAPPERS[name][0]}{path}', json=body,
                headers={'Authorization': f'Bearer {TOKEN}'}, timeout=aiohttp.ClientTimeout(total=20)) as response:
            raw = await response.text()
            assert response.status == (400 if oversized else 200), raw[:500]
            assert response.headers.get('X-Context-Recovery') == ('compact_input' if oversized else 'reduced_output')
            if not oversized:
                assert response.headers.get('X-Context-Output-Tokens') == '2000'
        records = [r for r in self.upstream.records[before:] if r['body']['model'] == model]
        assert len(records) == (1 if oversized else 3 if rotation else 2), records
        expected = {**body, 'tags': NOUS_REQUIRED_TAGS} if name == 'nous' else body
        assert records[0]['body'] == expected
        for record in records[1:]:
            assert record['body'] == {**expected, field: 2000}
        if not oversized:
            assert records[0]['key'] == records[1]['key']
            if rotation:
                assert records[2]['key'] != records[0]['key']
            if stream:
                frames = (self.upstream.responses_frames(body) if path == '/v1/responses' else
                          self.upstream.anthropic_frames(body) if path == '/v1/messages' else
                          self.upstream.chat_frames(body))
                assert raw == ''.join(frames), 'Recovery changed native SSE data'
        else:
            assert json.loads(raw)['error']['opaque'] == OPAQUE

    async def context_catalog(self, name):
        before = self.upstream.catalog_requests
        for _ in range(8):
            async with self.client.get(f'http://127.0.0.1:{WRAPPERS[name][0]}/v1/models') as response:
                assert response.status == 200
                models = (await response.json())['data']
                known = next(m for m in models if m['id'] == 'mock/normal')
                unknown = next(m for m in models if m['id'] == 'mock/unknown-context')
                assert known['context_window'] == known['context_length'] == 1048576
                assert known['max_output_tokens'] == 8192
                assert known['future_model_metadata'] == {'preserved': True}
                assert known['context_metadata']['status'] == 'known'
                assert unknown['context_metadata']['status'] == 'unknown'
                assert 'context_window' not in unknown
        assert self.upstream.catalog_requests - before <= 1, 'Discovery repeatedly fetched the upstream catalog'

    async def native(self, name, layer, stream):
        port = WRAPPERS[name][0]
        model = f"mock/{name}-native-layer{layer}-stream{int(stream)}"
        body = {"model": model, "messages": [{"role": "user", "content": "Check", "opaque_future_message": OPAQUE}],
                "stream": stream, "opaque_future_request": OPAQUE}
        if layer == 2:
            body.update({"max_tokens": 128, "output_config": {"effort": "high", "future": OPAQUE}})
        else:
            body.update({"reasoning_effort": "high", "max_completion_tokens": 128})
        before = len(self.upstream.records)
        status, raw = await self.post(port, "/v1/messages" if layer == 2 else "/v1/chat/completions", body,
                                      {"Authorization": f"Bearer stale", "x-api-key": TOKEN,
                                       "anthropic-version": "2023-06-01", "anthropic-beta": "future-feature-2027"})
        assert status == 200, f"HTTP {status}: {raw[:200]}"
        record = self.upstream.records[before:][-1]
        expected_body = {**body, "tags": NOUS_REQUIRED_TAGS} if name == "nous" else body
        assert record["body"] == expected_body, f"Native request was rewritten beyond provider metadata: {record['body']}"
        assert record["path"] == ("/v1/messages" if layer == 2 else "/v1/chat/completions")
        assert record["headers"].get("anthropic-beta") == "future-feature-2027"
        if stream:
            frames = self.upstream.anthropic_frames(body) if layer == 2 else self.upstream.chat_frames(body)
            expected = "".join(frames)
            assert raw == expected, "Native SSE frames were rewritten"
        else:
            expected = self.upstream.anthropic_reply(body) if layer == 2 else self.upstream.chat_reply(body)
            assert json.loads(raw) == expected, "Native JSON response fields were rewritten"

    async def sdk_messages(self, name, layer, stream):
        model = f"mock/{name}-tools-messages-layer{layer}-stream{int(stream)}"
        async with AsyncAnthropic(base_url=f"http://127.0.0.1:{WRAPPERS[name][0]}", api_key=TOKEN,
                                  max_retries=0, timeout=20) as client:
            params = {"model": model, "max_tokens": 128,
                      "messages": [{"role": "user", "content": "Use both tools"}],
                      "tools": [{"name": n, "input_schema": {"type": "object"}} for n, _ in TOOLS.values()]}
            if stream:
                async with client.messages.stream(**params) as response:
                    reply = await response.get_final_message()
            else:
                reply = await client.messages.create(**params)
            actual = {block.id: (block.name, block.input) for block in reply.content if block.type == "tool_use"}
            assert actual == TOOLS, f"Parallel tool identity/arguments changed: {actual}"
            assert any(block.type == "thinking" and block.thinking == "Checking both tools" for block in reply.content), "Reasoning lost"
            assert reply.stop_reason == "tool_use"

    async def sdk_chat(self, name, layer, stream):
        model = f"mock/{name}-tools-chat-layer{layer}-stream{int(stream)}"
        async with AsyncOpenAI(base_url=f"http://127.0.0.1:{WRAPPERS[name][0]}/v1", api_key=TOKEN,
                               max_retries=0, timeout=20) as client:
            reply = await client.chat.completions.create(model=model, stream=stream,
                messages=[{"role": "user", "content": "Use both tools"}],
                tools=[{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}} for n, _ in TOOLS.values()])
            if not stream:
                calls = {call.id: (call.function.name, json.loads(call.function.arguments)) for call in reply.choices[0].message.tool_calls}
                reasoning = getattr(reply.choices[0].message, "reasoning_content", None)
            else:
                by_index, reasoning = {}, ""
                async with reply:
                    async for chunk in reply:
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta
                        reasoning += getattr(delta, "reasoning_content", None) or ""
                        for call in delta.tool_calls or []:
                            saved = by_index.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
                            saved["id"] = call.id or saved["id"]
                            if call.function:
                                saved["name"] += call.function.name or ""
                                saved["arguments"] += call.function.arguments or ""
                calls = {c["id"]: (c["name"], json.loads(c["arguments"])) for c in by_index.values()}
            assert calls == TOOLS, f"Parallel tool identity/arguments changed: {calls}"
            assert reasoning == "Checking both tools", f"Reasoning changed: {reasoning!r}"

    async def sdk_responses(self, name, layer, stream):
        model = f"mock/{name}-tools-responses-layer{layer}-stream{int(stream)}"
        before = len(self.upstream.records)
        async with AsyncOpenAI(base_url=f"http://127.0.0.1:{WRAPPERS[name][0]}/v1", api_key=TOKEN,
                               max_retries=0, timeout=20) as client:
            params = {"model": model, "input": "Use both tools",
                      "tools": [{"type": "function", "name": n, "parameters": {"type": "object"}} for n, _ in TOOLS.values()]}
            if stream:
                async with client.responses.stream(**params) as response:
                    reply = await response.get_final_response()
            else:
                reply = await client.responses.create(**params)
            calls = {item.call_id: (item.name, json.loads(item.arguments)) for item in reply.output if item.type == "function_call"}
            assert calls == TOOLS, f"Parallel Responses tool identity/arguments changed: {calls}"
            assert any(item.type == "reasoning" for item in reply.output), "Responses reasoning lost"
            await client.responses.create(model=model, previous_response_id=reply.id,
                input=[{"type": "function_call_output", "call_id": call_id, "output": "done"} for call_id in TOOLS])
        requests = self.upstream.records[before:]
        assert len(requests) == 2, f"Unexpected continuation upstream attempts: {len(requests)}"
        messages = requests[-1]["body"]["messages"]
        assistant = [m for m in messages if m["role"] == "assistant"]
        assert assistant, "previous_response_id dropped assistant tool history"
        if layer == 1:
            ids = {call["id"] for m in assistant for call in m.get("tool_calls", [])}
            output_ids = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        else:
            ids = {block["id"] for m in assistant for block in m["content"] if block["type"] == "tool_use"}
            output_ids = {block["tool_use_id"] for m in messages if m["role"] == "user" and isinstance(m["content"], list)
                          for block in m["content"] if block["type"] == "tool_result"}
        assert ids == output_ids == set(TOOLS), f"Continuation lost tool call/output identity: {ids}, {output_ids}"
        assert all(record["body"]["model"] == model for record in requests), "Continuation changed requested model"

    async def retry(self, name, layer, status, stream):
        model = f"mock/{name}-layer{layer}-stream{int(stream)}/retry-{status}"
        before = len(self.upstream.records)
        code, raw = await self.post(WRAPPERS[name][0], "/v1/chat/completions",
                                    {"model": model, "stream": stream, "messages": [{"role": "user", "content": "Check"}]})
        records = self.upstream.records[before:]
        assert code == 200, f"HTTP {code} after {len(records)} attempts: {raw[:200]}"
        assert len(records) == 4 and len({r["key"] for r in records}) == 4, "Did not try all four available keys exactly once"
        assert all(r["body"]["model"] == model for r in records), "Retry changed model"

    async def cancel(self, name, layer):
        model = f"mock/{name}-layer{layer}/cancel"
        path = "/v1/messages" if layer == 2 else "/v1/chat/completions"
        body = {"model": model, "stream": True, "max_tokens": 128, "messages": [{"role": "user", "content": "Check"}]}
        async with self.client.post(f"http://127.0.0.1:{WRAPPERS[name][0]}{path}", json=body,
                                    headers={"Authorization": f"Bearer {TOKEN}"}) as reply:
            assert reply.status == 200
            assert await reply.content.readany(), "No first upstream event received"
        deadline = time.monotonic() + 3
        while model not in self.upstream.cancelled and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert model in self.upstream.cancelled, "Client disconnect left upstream generating"
        health_path = "/health" if name == "nous" else "/stats"
        async with self.client.get(f"http://127.0.0.1:{WRAPPERS[name][0]}{health_path}",
                                   headers={"Authorization": f"Bearer {TOKEN}"}) as reply:
            stats = await reply.json()
        keys = stats.get("live_keys") or stats.get("key_pool", {}).get("keys", [])
        assert len(keys) == 4, f"Cannot verify four key counters from {health_path}"
        assert all(key["in_flight"] == 0 for key in keys), "Disconnect leaked key capacity"

    async def unauthorized(self, name):
        before = len(self.upstream.records)
        code, _ = await self.post(WRAPPERS[name][0], "/v1/chat/completions",
                                 {"model": "mock/normal", "messages": [{"role": "user", "content": "Check"}]},
                                 {"Authorization": "Bearer invalid", "x-api-key": "invalid"})
        assert code == 401, f"Invalid client credentials returned HTTP {code}"
        assert len(self.upstream.records) == before, "Rejected client request reached upstream"

    async def exhausted(self, name, layer):
        model = f"mock/{name}-layer{layer}/exhaust"
        before = len(self.upstream.records)
        code, raw = await self.post(WRAPPERS[name][0], "/v1/chat/completions",
                                    {"model": model, "messages": [{"role": "user", "content": "Check"}]})
        records = self.upstream.records[before:]
        assert code == 429 and "error" in json.loads(raw), f"Unusable keys returned HTTP {code}: {raw[:200]}"
        assert len(records) == len({r["key"] for r in records}) == 4, "Key exhaustion did not try all four distinct keys"
        assert all(r["body"]["model"] == model for r in records), "Exhaustion changed requested model"

    async def native_responses(self, name, stream):
        model = f"mock/{name}/native-responses-stream{int(stream)}"
        body = {"model": model, "stream": stream,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Check"},
                            {"type": "future_input", "opaque": OPAQUE}]}],
                "previous_response_id": "resp_remote_previous_opaque", "store": False,
                "reasoning": {"effort": "xhigh", "opaque": OPAQUE}, "opaque_future_request": OPAQUE}
        before = len(self.upstream.records)
        code, raw = await self.post(WRAPPERS[name][0], "/v1/responses", body)
        assert code == 200, f"HTTP {code}: {raw[:200]}"
        records = self.upstream.records[before:]
        assert len(records) == 1 and records[0]["path"] == "/v1/responses", "Native Responses was translated to another API"
        expected_body = {**body, "tags": NOUS_REQUIRED_TAGS} if name == "nous" else body
        assert records[0]["body"] == expected_body, "Native Responses payload or previous_response_id was rewritten beyond provider metadata"
        if stream:
            assert raw == "".join(self.upstream.responses_frames(body)), "Native Responses SSE/unknown events were rewritten"
        else:
            assert json.loads(raw) == self.upstream.responses_reply(body), "Native Responses JSON fields were rewritten"

    async def native_responses_sdk(self, name, stream):
        model = f"mock/{name}/native-tools-responses-stream{int(stream)}"
        before = len(self.upstream.records)
        async with AsyncOpenAI(base_url=f"http://127.0.0.1:{WRAPPERS[name][0]}/v1", api_key=TOKEN,
                               max_retries=0, timeout=20) as client:
            params = {"model": model, "input": "Use both tools", "parallel_tool_calls": True,
                      "tools": [{"type": "function", "name": n, "parameters": {"type": "object"}} for n, _ in TOOLS.values()],
                      "extra_body": {"opaque_future_request": OPAQUE}}
            if stream:
                async with client.responses.stream(**params) as response:
                    reply = await response.get_final_response()
            else:
                reply = await client.responses.create(**params)
            calls = {item.call_id: (item.name, json.loads(item.arguments)) for item in reply.output if item.type == "function_call"}
            assert calls == TOOLS, f"Native SDK tool arguments changed: {calls}"
            assert any(item.type == "reasoning" for item in reply.output), "Native SDK reasoning lost"
            assert getattr(reply, "opaque_future_response", None) == OPAQUE, "Native SDK opaque response lost"
            await client.responses.create(model=model, previous_response_id=reply.id,
                input=[{"type": "function_call_output", "call_id": call_id, "output": "done"} for call_id in TOOLS])
        records = self.upstream.records[before:]
        assert len(records) == 2 and all(r["path"] == "/v1/responses" for r in records), "Native SDK route changed"
        assert records[-1]["body"]["previous_response_id"] == reply.id, "Native previous_response_id was locally rewritten"
        assert records[-1]["body"]["input"] == [{"type": "function_call_output", "call_id": cid, "output": "done"} for cid in TOOLS]

    async def native_responses_no_downgrade(self, name):
        before = len(self.upstream.records)
        model = f"mock/{name}/native-unavailable"
        code, raw = await self.post(WRAPPERS[name][0], "/v1/responses", {"model": model, "input": "Check"})
        records = self.upstream.records[before:]
        assert code == 404 and "error" in json.loads(raw), f"Native unsupported error changed: HTTP {code}"
        assert len(records) == 1 and records[0]["path"] == "/v1/responses", "Native failure silently downgraded to Chat"

    async def responses_auto_fallback(self, name, status, stream):
        before = len(self.upstream.records)
        model = f"mock/{name}/native-unavailable-{status}"
        async with AsyncOpenAI(base_url=f"http://127.0.0.1:{WRAPPERS[name][0]}/v1",
                              api_key=TOKEN, max_retries=0) as sdk:
            if stream:
                async with sdk.responses.stream(model=model, input="Check approved adapter") as response:
                    async for _ in response:
                        pass
                    final = await response.get_final_response()
            else:
                final = await sdk.responses.create(model=model, input="Check approved adapter")
        assert final.status == "completed" and final.output_text == "Acceptance answer"
        records = self.upstream.records[before:]
        assert len(records) == 2, "Unsupported endpoint consumed more credentials than necessary"
        assert [record["path"] for record in records] == ["/v1/responses", "/v1/chat/completions"]
        assert all(record["body"]["model"] == model for record in records), "Adapter changed model"

    async def responses_auto_unknown_history(self, name):
        before = len(self.upstream.records)
        model = f"mock/{name}/native-unavailable-405"
        code, raw = await self.post(WRAPPERS[name][0], "/v1/responses", {
            "model": model, "input": "Next turn", "previous_response_id": "native-upstream-owned-history"})
        assert code == 400 and json.loads(raw)["error"]["code"] == "previous_response_not_found"
        records = self.upstream.records[before:]
        assert len(records) == 1 and records[0]["path"] == "/v1/responses", "Unknown history was silently discarded"


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wrapper", choices=WRAPPERS, action="append")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    names = args.wrapper or list(WRAPPERS)
    assert_ports_free(UPSTREAM_PORT, *(WRAPPERS[name][0] for name in names))
    upstream = Upstream()
    app = web.Application()
    app.router.add_get("/v1/models", upstream.models)
    app.router.add_post("/v1/chat/completions", upstream.inference)
    app.router.add_post("/v1/messages", upstream.inference)
    app.router.add_post("/v1/responses", upstream.inference)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", UPSTREAM_PORT).start()
    with tempfile.TemporaryDirectory(prefix="wrapper-acceptance-logs-") as log_dir:
        async with aiohttp.ClientSession() as client:
            acceptance = Acceptance(upstream, client)
            try:
                for name in names:
                    for layer in (1, 2):
                        process, log = start_wrapper(name, layer, Path(log_dir))
                        prefix = f"{name}/layer{layer}"
                        try:
                            await wait_health(client, WRAPPERS[name][0], process)
                            await acceptance.check(prefix + "/auth", lambda: acceptance.unauthorized(name))
                            await acceptance.check(prefix + '/context-catalog-cache', lambda: acceptance.context_catalog(name))
                            for stream in (False, True):
                                suffix = f"/stream{int(stream)}"
                                native_path = '/v1/messages' if layer == 2 else '/v1/chat/completions'
                                await acceptance.check(prefix + '/context-recovery' + suffix,
                                    lambda s=stream: acceptance.context_recovery(name, native_path, s))
                                await acceptance.check(prefix + '/context-input-overflow' + suffix,
                                    lambda s=stream: acceptance.context_recovery(name, native_path, s, oversized=True))
                                await acceptance.check(prefix + '/context-rotation' + suffix,
                                    lambda s=stream: acceptance.context_recovery(name, native_path, s, rotation=True))
                                await acceptance.check(prefix + "/native" + suffix, lambda s=stream: acceptance.native(name, layer, s))
                                await acceptance.check(prefix + "/sdk-chat-tools" + suffix, lambda s=stream: acceptance.sdk_chat(name, layer, s))
                                await acceptance.check(prefix + "/sdk-messages-tools" + suffix, lambda s=stream: acceptance.sdk_messages(name, layer, s))
                                await acceptance.check(prefix + "/sdk-responses-continuation" + suffix, lambda s=stream: acceptance.sdk_responses(name, layer, s))
                            await acceptance.check(prefix + "/disconnect", lambda: acceptance.cancel(name, layer))
                            # Context+rotation checks intentionally cooled keys.
                            # Let their controlled 1s cooldown expire before a
                            # separate contract requires all four credentials.
                            await asyncio.sleep(1.2)
                            for status in (429, 401, 402):
                                for stream in (False, True):
                                    await acceptance.check(prefix + f"/all-keys-{status}/stream{int(stream)}",
                                                           lambda status=status, stream=stream: acceptance.retry(name, layer, status, stream))
                                    await asyncio.sleep(1.2)  # controlled 1s cooldown; no production waits
                            await acceptance.check(prefix + "/key-exhaustion", lambda: acceptance.exhausted(name, layer))
                        except Exception as exc:
                            acceptance.checks.append({"check": prefix + "/startup", "passed": False, "error": str(exc)})
                            print(f"FAIL {prefix}/startup: {exc}", flush=True)
                        finally:
                            await asyncio.to_thread(stop_process, process)
                            log.close()
                            if any(not c["passed"] and c["check"].startswith(prefix) for c in acceptance.checks):
                                print(f"Wrapper evidence: {log.name}", flush=True)
                                if args.output:
                                    target = args.output.parent / Path(log.name).name
                                    target.parent.mkdir(parents=True, exist_ok=True)
                                    target.write_text(Path(log.name).read_text(errors="replace"))
                    process, log = start_wrapper(name, 1, Path(log_dir), response_mode="native")
                    prefix = name + "/native-responses-strict"
                    try:
                        await wait_health(client, WRAPPERS[name][0], process)
                        for stream in (False, True):
                            await acceptance.check(prefix + f'/context-recovery/stream{int(stream)}',
                                lambda s=stream: acceptance.context_recovery(name, '/v1/responses', s))
                            await acceptance.check(prefix + f'/context-rotation/stream{int(stream)}',
                                lambda s=stream: acceptance.context_recovery(name, '/v1/responses', s, rotation=True))
                            await acceptance.check(prefix + f"/opaque/stream{int(stream)}",
                                                   lambda stream=stream: acceptance.native_responses(name, stream))
                            await acceptance.check(prefix + f"/sdk-tools-continuation/stream{int(stream)}",
                                                   lambda stream=stream: acceptance.native_responses_sdk(name, stream))
                        await acceptance.check(prefix + "/unsupported-no-downgrade",
                                               lambda: acceptance.native_responses_no_downgrade(name))
                    except Exception as exc:
                        acceptance.checks.append({"check": prefix + "/startup", "passed": False, "error": str(exc)})
                        print(f"FAIL {prefix}/startup: {exc}", flush=True)
                    finally:
                        await asyncio.to_thread(stop_process, process)
                        log.close()
                        if args.output and any(not c["passed"] and c["check"].startswith(prefix) for c in acceptance.checks):
                            target = args.output.parent / Path(log.name).name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(Path(log.name).read_text(errors="replace"))
                    process, log = start_wrapper(name, 1, Path(log_dir), response_mode=None)
                    prefix = name + "/responses-auto-default"
                    try:
                        await wait_health(client, WRAPPERS[name][0], process)
                        await acceptance.check(prefix + '/native-first', lambda: acceptance.native_responses(name, False))
                        for status in (404, 405, 501):
                            for stream in (False, True):
                                await acceptance.check(prefix + f'/unsupported-{status}/stream{int(stream)}',
                                    lambda status=status, stream=stream: acceptance.responses_auto_fallback(name, status, stream))
                        await acceptance.check(prefix + '/unknown-history', lambda: acceptance.responses_auto_unknown_history(name))
                    except Exception as exc:
                        acceptance.checks.append({'check': prefix + '/startup', 'passed': False, 'error': str(exc)})
                        print(f'FAIL {prefix}/startup: {exc}', flush=True)
                    finally:
                        await asyncio.to_thread(stop_process, process)
                        log.close()
                        if args.output and any(not c['passed'] and c['check'].startswith(prefix) for c in acceptance.checks):
                            target = args.output.parent / Path(log.name).name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(Path(log.name).read_text(errors='replace'))
            finally:
                await runner.cleanup()
    result = {"scope": names, "upstream": "isolated-local-mock", "checks": acceptance.checks,
              "passed": sum(c["passed"] for c in acceptance.checks),
              "failed": sum(not c["passed"] for c in acceptance.checks)}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Acceptance: {result['passed']} passed, {result['failed']} failed", flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
