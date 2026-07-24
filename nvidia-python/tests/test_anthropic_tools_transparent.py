#!/usr/bin/env python3
"""Claude Code transparency: tool_use/tool_result must stay structured, never DSML text."""

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running without package install
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LOG_FILE", "/tmp/wrapper-audit-logs/nvidia.log")
os.environ.setdefault("EVENTS_FILE", "/tmp/wrapper-audit-logs/e.jsonl")
os.environ.setdefault("METRICS_DB", "/tmp/wrapper-audit-logs/m.db")
os.environ.setdefault("VERIFY_ON_BOOT", "false")

from src.anthropic_compat import (  # noqa: E402
    anthropic_to_openai,
    openai_to_anthropic,
    stream_openai_to_anthropic,
)


def test_a2o_tool_use_not_dsml():
    body = {
        "model": "minimaxai/minimax-m3",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "need bash"},
                    {"type": "text", "text": "I will list"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls -la"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "total 0",
                    }
                ],
            },
        ],
        "tools": [
            {
                "name": "Bash",
                "description": "run",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                },
            }
        ],
        "thinking": {"type": "enabled"},
    }
    oai = anthropic_to_openai(body)
    assert "error" not in oai, oai
    blob = json.dumps(oai)
    assert "DSML" not in blob
    assert "tool_calls>" not in blob

    asst = [m for m in oai["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst, oai["messages"]
    assert asst[0]["tool_calls"][0]["function"]["name"] == "Bash"
    assert asst[0].get("reasoning_content") == "need bash"

    tools = [m for m in oai["messages"] if m.get("role") == "tool"]
    assert tools and tools[0]["tool_call_id"] == "toolu_1"
    assert tools[0]["content"] == "total 0"
    assert oai["tools"][0]["function"]["name"] == "Bash"
    assert oai["tools"][0]["function"]["parameters"]["type"] == "object"


def test_o2a_tool_calls_structured():
    resp = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "Bash",
                                "arguments": '{"command":"pwd"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    a = openai_to_anthropic(resp, "minimaxai/minimax-m3", expect_thinking=False)
    assert a["stop_reason"] == "tool_use"
    types = [c["type"] for c in a["content"]]
    assert "tool_use" in types
    assert "DSML" not in json.dumps(a)
    tu = next(c for c in a["content"] if c["type"] == "tool_use")
    assert tu["name"] == "Bash"
    assert tu["input"]["command"] == "pwd"


def test_o2a_thinking_not_concatenated_into_text():
    resp = {
        "choices": [
            {
                "message": {
                    "content": "hello",
                    "reasoning_content": "plan",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    a = openai_to_anthropic(resp, "m", expect_thinking=True)
    types = [c["type"] for c in a["content"]]
    assert types[0] == "thinking"
    assert a["content"][0]["thinking"] == "plan"
    text_blocks = [c for c in a["content"] if c["type"] == "text"]
    assert text_blocks and text_blocks[0]["text"] == "hello"
    assert "plan" not in text_blocks[0]["text"]


def test_stream_tool_calls_emit_anthropic_tool_use():
    async def upstream():
        seq = [
            {"choices": [{"delta": {"role": "assistant"}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "Bash", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '{"command":"ls"}'},
                                }
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        for s in seq:
            yield ("data: " + json.dumps(s) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def collect():
        out = []
        async for x in stream_openai_to_anthropic(
            upstream(), "m", {}, expect_thinking=False
        ):
            out.append(x)
        return out

    out = asyncio.run(collect())
    blob = "".join(out)
    assert "message_start" in blob
    assert "tool_use" in blob
    assert "input_json_delta" in blob
    assert "message_stop" in blob
    assert "DSML" not in blob
    assert "tool_use" in blob


def test_stream_error_still_closes():
    async def bad_upstream():
        yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        raise RuntimeError("upstream dropped")

    async def collect():
        out = []
        async for x in stream_openai_to_anthropic(bad_upstream(), "m", {}):
            out.append(x)
        return out

    out = asyncio.run(collect())
    blob = "".join(out)
    assert "message_start" in blob
    assert "message_stop" in blob
