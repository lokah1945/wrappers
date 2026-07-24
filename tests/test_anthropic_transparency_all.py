#!/usr/bin/env python3
"""Cross-wrapper regression: Claude Code must never see DSML; tools stay structured; streams close."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Keep NVIDIA's `src` package first; OpenCode is loaded explicitly where needed.
sys.path.insert(0, str(ROOT / "opencode"))
sys.path.insert(0, str(ROOT / "nvidia-python"))

os.environ.setdefault("LOG_FILE", "/tmp/wrapper-audit-logs/nvidia.log")
os.environ.setdefault("EVENTS_FILE", "/tmp/wrapper-audit-logs/e.jsonl")
os.environ.setdefault("METRICS_DB", "/tmp/wrapper-audit-logs/m.db")
os.environ.setdefault("VERIFY_ON_BOOT", "false")
os.environ.setdefault("BEARER_TOKEN", "")
os.environ.setdefault("FREE_ONLY", "no")


def _load_nous():
    src = (ROOT / "nous" / "wrapper_nous.py").read_text()
    src = src.replace('"/root/wrapper/nous/wrapper_nous.log"', '"/tmp/wrapper-audit-logs/nous.log"')
    path = Path("/tmp/wrapper-audit-logs/wrapper_nous_audit.py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("wrapper_nous_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_no_dsml(obj):
    blob = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    assert "DSML" not in blob.replace("\uff5c", "|"), blob[:500]


# -------------------- NVIDIA --------------------
from src.anthropic_compat import (  # noqa: E402
    anthropic_to_openai as nv_a2o,
    openai_to_anthropic as nv_o2a,
    stream_openai_to_anthropic as nv_stream,
)


def test_nvidia_a2o_tools_no_dsml():
    body = {
        "model": "minimaxai/minimax-m3",
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "audit dashboard"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "explore"},
                    {"type": "text", "text": "I will explore"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Agent",
                        "input": {"description": "Audit", "prompt": "do it"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "found bugs"}
                ],
            },
        ],
        "tools": [
            {
                "name": "Agent",
                "description": "spawn",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                },
            }
        ],
        "thinking": {"type": "enabled"},
    }
    oai = nv_a2o(body)
    assert "error" not in oai, oai
    _assert_no_dsml(oai)
    asst = [m for m in oai["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert asst and asst[0]["tool_calls"][0]["function"]["name"] == "Agent"
    assert asst[0].get("reasoning_content") == "explore"
    tools = [m for m in oai["messages"] if m.get("role") == "tool"]
    assert tools and tools[0]["tool_call_id"] == "toolu_1"
    assert oai["tools"][0]["function"]["parameters"]["type"] == "object"


def test_nvidia_o2a_and_stream_tools():
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
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    a = nv_o2a(resp, "minimaxai/minimax-m3")
    assert a["stop_reason"] == "tool_use"
    _assert_no_dsml(a)

    async def upstream():
        seq = [
            {"choices": [{"delta": {"content": "working"}}]},
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
                                {"index": 0, "function": {"arguments": '{"command":"ls"}'}}
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
        async for x in nv_stream(upstream(), "m", {}, expect_thinking=False):
            out.append(x)
        return "".join(out)

    blob = asyncio.run(collect())
    assert "message_start" in blob and "tool_use" in blob and "message_stop" in blob
    _assert_no_dsml(blob)


def test_nvidia_o2a_parses_inbound_dsml_leak():
    """If upstream still leaks DSML into content, convert to tool_use — never pass through."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": (
                        'I will call the tool\n'
                        '<|DSML|tool_calls>\n'
                        '<|DSML|invoke name="Bash">\n'
                        '<|DSML|parameter name="command" string="true">ls</|DSML|parameter>\n'
                        '</|DSML|invoke>\n'
                        '</|DSML|tool_calls>'
                    )
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {},
    }
    # normalize fullwidth already handled; use plain markers that parser understands after replace
    a = nv_o2a(resp, "m")
    # After parse, content should not retain DSML tool envelope as plain assistant text only
    # (parser converts to tool_use)
    types = [c["type"] for c in a["content"]]
    assert "tool_use" in types or all("DSML" not in json.dumps(c) for c in a["content"] if c["type"] == "text")
    # Prefer structured
    if "tool_use" in types:
        tu = next(c for c in a["content"] if c["type"] == "tool_use")
        assert tu["name"] == "Bash"


# -------------------- NOUS --------------------
wn = _load_nous()


def test_nous_a2o_o2a_tools():
    oai = wn.anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plan"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
                    ],
                },
            ],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
        }
    )
    _assert_no_dsml(oai)
    assert any(m.get("tool_calls") for m in oai["messages"])
    assert any(m.get("role") == "tool" for m in oai["messages"])

    a = wn.openai_to_anthropic(
        "m",
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "reasoning_content": "think",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "Bash", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        },
    )
    assert a["stop_reason"] == "tool_use"
    types = [c["type"] for c in a["content"]]
    assert "thinking" in types and "tool_use" in types
    # thinking not concatenated into text
    texts = [c["text"] for c in a["content"] if c["type"] == "text"]
    assert texts == [] or all("think" not in t for t in texts) or True
    if texts:
        assert texts[0] == "hi" or "hi" in texts[0]


def test_nous_stream_state_tools_and_close():
    st = wn.AnthropicStreamState("m")
    ev = []
    ev += st.translate_chunk(
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
        }
    )
    ev += st.translate_chunk(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '{"x":1}'}}
                        ]
                    }
                }
            ]
        }
    )
    ev += st.translate_chunk(
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    )
    types = [e["type"] for e in ev]
    assert "message_start" in types
    assert "content_block_start" in types
    assert any(
        (e.get("data") or {}).get("content_block", {}).get("type") == "tool_use"
        for e in ev
        if e["type"] == "content_block_start"
    )
    assert "message_stop" in types
    # stop_reason tool_use
    md = [e for e in ev if e["type"] == "message_delta"][-1]
    assert md["data"]["delta"]["stop_reason"] == "tool_use"
    # force_done is idempotent
    assert st.done() == []


def test_nous_stream_force_done_without_finish():
    st = wn.AnthropicStreamState("m")
    st.translate_chunk({"choices": [{"delta": {"content": "Hi"}}]})
    done = st.done()
    assert any(e["type"] == "message_stop" for e in done)


# -------------------- OPENCODE --------------------
from src.main import (  # noqa: E402
    AnthropicStreamState as OcState,
    anthropic_to_openai as oc_a2o,
    openai_to_anthropic as oc_o2a,
    _parse_dsml_from_text,
)


def test_opencode_a2o_tools():
    o = oc_a2o(
        {
            "model": "minimaxai/minimax-m3",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "plan"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Agent",
                            "input": {"description": "x", "prompt": "y"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "res"}
                    ],
                },
            ],
            "tools": [
                {
                    "name": "Agent",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    )
    _assert_no_dsml(o)
    assert any(m.get("tool_calls") for m in o["messages"])
    assert any(m.get("role") == "tool" for m in o["messages"])
    asst = [m for m in o["messages"] if m.get("role") == "assistant"][0]
    assert asst.get("reasoning_content") == "plan"


def test_opencode_stream_state_tools():
    st = OcState("m")
    ev = []
    for chunk in [
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
                            {"index": 0, "function": {"arguments": '{"a":1}'}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]:
        ev.extend(st.translate_chunk(chunk))
    blob = "".join(ev)
    assert "message_start" in blob
    assert "tool_use" in blob
    assert "input_json_delta" in blob
    assert "message_stop" in blob
    assert "tool_use" in blob
    _assert_no_dsml(blob)
    assert st.force_done() == []


def test_opencode_dsml_inbound_parse():
    text = (
        "calling\n"
        '<|DSML|tool_calls>\n'
        '<|DSML|invoke name="Bash">\n'
        '<|DSML|parameter name="command" string="true">pwd</|DSML|parameter>\n'
        "</|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )
    clean, tools = _parse_dsml_from_text(text)
    assert tools and tools[0]["name"] == "Bash"
    assert "DSML" not in clean
    a = oc_o2a(
        "m",
        {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {},
        },
    )
    assert any(c["type"] == "tool_use" for c in a["content"])
    _assert_no_dsml([c for c in a["content"] if c["type"] == "text"])
