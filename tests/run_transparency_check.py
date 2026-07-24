#!/usr/bin/env python3
"""Cross-wrapper Anthropic/Claude Code transparency checks (no DSML leak, tools structured, streams close)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGDIR = Path("/tmp/wrapper-audit-logs")
LOGDIR.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("LOG_FILE", str(LOGDIR / "nvidia.log"))
os.environ.setdefault("EVENTS_FILE", str(LOGDIR / "e.jsonl"))
os.environ.setdefault("METRICS_DB", str(LOGDIR / "m.db"))
os.environ.setdefault("VERIFY_ON_BOOT", "false")
os.environ.setdefault("BEARER_TOKEN", "")
os.environ.setdefault("FREE_ONLY", "no")


def no_dsml(obj) -> None:
    blob = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    assert "DSML" not in blob.replace("\uff5c", "|"), blob[:400]


def load_nous():
    src = (ROOT / "nous" / "wrapper_nous.py").read_text()
    src = src.replace(
        '"/root/wrapper/nous/wrapper_nous.log"',
        f'"{LOGDIR / "nous.log"}"',
    )
    path = LOGDIR / "wrapper_nous_audit.py"
    path.write_text(src)
    spec = importlib.util.spec_from_file_location("wrapper_nous_audit", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    # ---------- NVIDIA ----------
    sys.path.insert(0, str(ROOT / "nvidia-python"))
    from src.anthropic_compat import (  # type: ignore
        anthropic_to_openai as nv_a2o,
        openai_to_anthropic as nv_o2a,
        stream_openai_to_anthropic as nv_stream,
    )

    oai = nv_a2o(
        {
            "model": "minimaxai/minimax-m3",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "p"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Agent",
                            "input": {"description": "d", "prompt": "p"},
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
            "tools": [{"name": "Agent", "input_schema": {"type": "object"}}],
            "thinking": {"type": "enabled"},
        }
    )
    no_dsml(oai)
    assert any(m.get("tool_calls") for m in oai["messages"])
    assert any(m.get("role") == "tool" for m in oai["messages"])
    print("NV A→O OK")

    a = nv_o2a(
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "Bash",
                                    "arguments": json.dumps({"command": "ls"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        },
        "m",
    )
    assert a["stop_reason"] == "tool_use"
    no_dsml(a)
    print("NV O→A OK")

    async def up():
        seq = [
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
                                    "function": {
                                        "arguments": json.dumps({"x": 1})
                                    },
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

    async def collect_nv():
        out = []
        async for x in nv_stream(up(), "m", {}, expect_thinking=False):
            out.append(x)
        return "".join(out)

    blob = asyncio.run(collect_nv())
    assert "tool_use" in blob and "message_stop" in blob
    no_dsml(blob)
    print("NV STREAM OK")

    # ---------- NOUS ----------
    wn = load_nous()
    oai = wn.anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"c": "ls"},
                        }
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
    assert any(m.get("tool_calls") for m in oai["messages"])
    assert any(m.get("role") == "tool" for m in oai["messages"])
    a = wn.openai_to_anthropic(
        "m",
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "reasoning_content": "r",
                        "tool_calls": [
                            {
                                "id": "c",
                                "function": {"name": "B", "arguments": "{}"},
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
    assert a["content"][0]["type"] == "thinking"
    assert a["content"][1]["text"] == "hi"

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
                            {"index": 0, "function": {"arguments": "{}"}}
                        ]
                    }
                }
            ]
        }
    )
    ev += st.translate_chunk(
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}
    )
    assert any(e["type"] == "message_stop" for e in ev)
    assert (
        [e for e in ev if e["type"] == "message_delta"][-1]["data"]["delta"][
            "stop_reason"
        ]
        == "tool_use"
    )
    assert st.done() == []
    print("NOUS OK")

    # ---------- OPENCODE ----------
    for k in list(sys.modules):
        if k == "src" or k.startswith("src."):
            del sys.modules[k]
    sys.path = [p for p in sys.path if "nvidia-python" not in p]
    sys.path.insert(0, str(ROOT / "opencode"))
    os.environ["LOG_FILE"] = str(Path("/tmp/wrapper-audit-logs/oc.log"))
    os.environ["LOG_FILE"] = str(Path("/tmp/wrapper-audit-logs/oc.log"))
    import src.main as oc  # type: ignore

    o = oc.anthropic_to_openai(
        {
            "model": "m",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "p"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Agent",
                            "input": {"description": "d", "prompt": "p"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "r"}
                    ],
                },
            ],
            "tools": [{"name": "Agent", "input_schema": {"type": "object"}}],
        }
    )
    assert any(m.get("tool_calls") for m in o["messages"])
    assert (
        [m for m in o["messages"] if m.get("role") == "assistant"][0].get(
            "reasoning_content"
        )
        == "p"
    )

    st = oc.AnthropicStreamState("m")
    evs = []
    for ch in [
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
        evs.extend(st.translate_chunk(ch))
    blob = "".join(evs)
    assert "tool_use" in blob and "message_stop" in blob
    no_dsml(blob)

    text = (
        "x\n"
        "<|DSML|tool_calls>\n"
        '<|DSML|invoke name="Bash">\n'
        '<|DSML|parameter name="command" string="true">pwd</|DSML|parameter>\n'
        "</|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )
    clean, tools = oc._parse_dsml_from_text(text)
    assert tools and tools[0]["name"] == "Bash"
    a = oc.openai_to_anthropic(
        "m",
        {
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {},
        },
    )
    assert any(c["type"] == "tool_use" for c in a["content"])
    print("OPENCODE OK")
    print("ALL CROSS-WRAPPER TRANSPARENCY CHECKS PASS")


if __name__ == "__main__":
    main()
