#!/usr/bin/env python3
"""Tests for universal protocol conversion matrix across all wrappers."""

import pytest
import json
import asyncio
from common.translations import (
    anthropic_to_openai_response,
    openai_to_anthropic_response,
    stream_anthropic_to_openai,
    parse_dsml_from_text,
    repair_orphan_tool_messages,
    AnthropicStreamState,
)


def test_anthropic_to_openai_response_conversion():
    a_resp = {
        "id": "msg_12345",
        "type": "message",
        "role": "assistant",
        "model": "claude-3-5-sonnet-20241022",
        "content": [
            {"type": "thinking", "thinking": "Let me think about this."},
            {"type": "text", "text": "Hello world!"},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"location": "Berlin"}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 20}
    }

    oai = anthropic_to_openai_response(a_resp)
    assert oai["object"] == "chat.completion"
    assert oai["id"] == "chatcmpl-msg_12345"
    assert oai["model"] == "claude-3-5-sonnet-20241022"
    
    choice = oai["choices"][0]
    msg = choice["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Hello world!"
    assert msg["reasoning_content"] == "Let me think about this."
    assert choice["finish_reason"] == "tool_calls"
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"location": "Berlin"}
    assert oai["usage"]["prompt_tokens"] == 10
    assert oai["usage"]["completion_tokens"] == 20


def test_anthropic_to_openai_response_passthrough_when_already_openai():
    oai_resp = {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}]
    }
    res = anthropic_to_openai_response(oai_resp)
    assert res == oai_resp  # No conversion performed!


def test_openai_to_anthropic_response_conversion():
    oai_resp = {
        "id": "chatcmpl-999",
        "object": "chat.completion",
        "model": "meta/llama-3.1-8b-instruct",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello user!",
                "reasoning_content": "Internal reasoning step",
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "calculate", "arguments": '{"x": 42}'}
                }]
            },
            "finish_reason": "tool_calls"
        }],
        "usage": {"prompt_tokens": 15, "completion_tokens": 25}
    }

    a_resp = openai_to_anthropic_response(oai_resp)
    assert a_resp["type"] == "message"
    assert a_resp["id"] == "msg_chatcmpl-999"
    assert a_resp["role"] == "assistant"
    assert a_resp["stop_reason"] == "tool_use"
    
    content = a_resp["content"]
    types = [c["type"] for c in content]
    assert types == ["thinking", "text", "tool_use"]
    assert content[0]["thinking"] == "Internal reasoning step"
    assert content[1]["text"] == "Hello user!"
    assert content[2]["name"] == "calculate"
    assert content[2]["input"] == {"x": 42}
    assert a_resp["usage"]["input_tokens"] == 15
    assert a_resp["usage"]["output_tokens"] == 25


def test_openai_to_anthropic_response_passthrough_when_already_anthropic():
    anthro = {
        "id": "msg_existing",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "Already Anthropic"}]
    }
    res = openai_to_anthropic_response(anthro)
    assert res == anthro  # No conversion performed!


@pytest.mark.asyncio
async def test_stream_anthropic_to_openai():
    async def sample_anthropic_stream():
        yield "event: message_start\ndata: {\"type\": \"message_start\", \"message\": {\"id\": \"msg_1\"}}\n\n"
        yield "event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"index\": 0, \"delta\": {\"type\": \"text_delta\", \"text\": \"Hello \"}}\n\n"
        yield "event: content_block_delta\ndata: {\"type\": \"content_block_delta\", \"index\": 0, \"delta\": {\"type\": \"text_delta\", \"text\": \"world!\"}}\n\n"
        yield "event: message_delta\ndata: {\"type\": \"message_delta\", \"delta\": {\"stop_reason\": \"end_turn\"}, \"usage\": {\"input_tokens\": 5, \"output_tokens\": 10}}\n\n"
        yield "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"

    chunks = []
    async for chunk in stream_anthropic_to_openai(sample_anthropic_stream(), model="test-model"):
        chunks.append(chunk)

    combined = "".join(chunks)
    assert "data: " in combined
    assert "Hello " in combined
    assert "world!" in combined
    assert "data: [DONE]" in combined


def test_dsml_parsing():
    text = "Here is text <|DSML|tool_calls><|DSML|invoke name=\"run_code\"><|DSML|parameter name=\"code\">print('hi')</|DSML|parameter></|DSML|invoke></|DSML|tool_calls> after text"
    clean, tools = parse_dsml_from_text(text)
    assert "DSML" not in clean
    assert clean == "Here is text  after text"
    assert len(tools) == 1
    assert tools[0]["name"] == "run_code"
    assert tools[0]["input"] == {"code": "print('hi')"}


def test_orphan_tool_message_repair():
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "tool", "tool_call_id": "orphan_call_1", "content": "Result data"}
    ]
    repaired = repair_orphan_tool_messages(messages)
    assert len(repaired) == 2
    assert repaired[1]["role"] == "user"
    assert "Tool result for orphan_call_1: Result data" in repaired[1]["content"]


def test_anthropic_stream_state_idempotency_and_robustness():
    state = AnthropicStreamState(model="test-model")
    evs1 = state.translate_chunk({"choices": [{"delta": {"content": "Hello"}}]})
    assert len(evs1) > 0
    assert any("message_start" in e for e in evs1)

    evs2 = state.translate_chunk({"choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}]})
    assert any("message_stop" in e for e in evs2)

    # Subsequent chunk after finish_reason should NOT emit new content_block_start
    evs_after = state.translate_chunk({"choices": [{"delta": {"content": "extra"}}]})
    assert evs_after == []

    # force_done when already finished should be empty
    assert state.force_done() == []
