"""A terminal event must expose history ready for the next agent tool turn."""

import ast
import asyncio
import json
import logging
import random
import string
import time
from types import SimpleNamespace

import pytest

from common.sse import IDLE, iter_chunks_with_idle, normalize_sse_newlines
from tests.test_production_protocol_regressions import ROOT, _chunk, _frames, _load, _Upstream


def generator_for(wrapper, chunks, saved, releases):
    upstream = _Upstream([
        ("data: " + json.dumps(chunk) + "\n\n").encode() for chunk in chunks
    ] + [b"data: [DONE]\n\n"])
    namespace = {
        "json": json, "time": time, "asyncio": asyncio, "random": random, "string": string,
        "logger": logging.getLogger("terminal-persistence"), "HEARTBEAT_MS": 100000,
        "_iter_chunks_with_idle": iter_chunks_with_idle, "_IDLE": IDLE,
        "_normalize_sse_newlines": normalize_sse_newlines,
        "pool": SimpleNamespace(release=lambda key: releases.append(key)),
        "_store_response": lambda principal, rid, messages: saved.update({(principal, rid): messages}),
    }
    path = ROOT / wrapper / "src/main.py"
    _load(path, {"_assistant_message_from_chat"}, namespace)
    chat_body = {"messages": [{"role": "user", "content": "Use alpha"}]}
    if wrapper == "blackbox":
        _load(path, {"_responses_stream", "_emit_response_event"}, namespace)
        return namespace["_responses_stream"](upstream, "key", "turn1", "model", chat_body, "tenant"), upstream

    route = next(node for node in ast.parse(path.read_text()).body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "responses")
    generator_node = next(node for node in ast.walk(route)
                          if isinstance(node, ast.AsyncFunctionDef) and node.name == "gen"
                          and "process_payload" in {getattr(child, "name", "") for child in ast.walk(node)})
    namespace.update({"rid": "turn1", "model": "model", "resp": upstream, "key": "key",
                      "principal": "tenant", "chat_body": chat_body})

    async def chunk_stream(response):
        async for data in response:
            yield False, data

    namespace["_chunk_stream"] = chunk_stream
    exec(compile(ast.fix_missing_locations(ast.Module(body=[generator_node], type_ignores=[])),
                 str(path), "exec"), namespace)
    return namespace["gen"](), upstream


@pytest.mark.parametrize("wrapper", ["blackbox", "opencode"])
async def test_completed_turn_is_stored_before_client_closes_at_terminal(wrapper):
    saved, releases = {}, []
    generator, upstream = generator_for(wrapper, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_a", "function": {
            "name": "alpha", "arguments": '{"x":1}'}}]}), _chunk(finish="tool_calls"),
    ], saved, releases)
    try:
        async for frame in generator:
            if any(isinstance(event, dict) and event.get("type") == "response.completed"
                   for event in _frames([frame])):
                assert ("tenant", "turn1") in saved
                break
        else:
            pytest.fail("completed response was never emitted")
    finally:
        await generator.aclose()
    history = saved[("tenant", "turn1")]
    assert [message["role"] for message in history] == ["user", "assistant"]
    tool_call = history[-1]["tool_calls"][0]
    assert tool_call["id"] == "call_a"
    assert tool_call["function"] == {"name": "alpha", "arguments": '{"x":1}'}
    assert releases == ["key"]
    assert upstream.closed


@pytest.mark.parametrize("wrapper", ["blackbox", "opencode"])
async def test_failed_turn_is_not_stored_even_when_client_closes_at_terminal(wrapper):
    saved, releases = {}, []
    generator, upstream = generator_for(wrapper, [
        _chunk({"content": "partial"}), {"error": {"message": "upstream failed"}},
    ], saved, releases)
    try:
        async for frame in generator:
            if any(isinstance(event, dict) and event.get("type") == "response.failed"
                   for event in _frames([frame])):
                break
        else:
            pytest.fail("failed response was never emitted")
    finally:
        await generator.aclose()
    assert saved == {}
    assert releases == ["key"]
    assert upstream.closed
