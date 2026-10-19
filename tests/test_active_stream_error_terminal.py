"""Regression tests for surfaced upstream stream failures.

An Anthropic ``error`` SSE event is terminal. It must never be followed by a
synthetic ``message_delta(stop_reason=end_turn)`` or ``message_stop`` that
makes Claude Code persist a failed/truncated turn as a successful answer.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys
import types

from common.translations.anthropic_stream import AnthropicStreamState

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _event_type(raw: str) -> str:
    return json.loads(raw.split("data: ", 1)[1])["type"]


def _load_nvidia_compat():
    # nvidia-python is intentionally not importable as a normal Python package
    # because its directory contains a hyphen. Load it under an isolated package
    # name so the relative ``.capabilities`` import remains valid.
    sys.path.insert(0, str(ROOT))
    src = ROOT / "nvidia-python" / "src"
    package = types.ModuleType("nvidia_stream_error_test_pkg")
    package.__path__ = [str(src)]
    sys.modules[package.__name__] = package
    name = package.__name__ + ".anthropic_compat"
    spec = importlib.util.spec_from_file_location(name, src / "anthropic_compat.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_shared_anthropic_state_does_not_close_error_as_success():
    state = AnthropicStreamState("model")
    events = state.translate_chunk({
        "error": {"type": "server_error", "message": "synthetic upstream failure"}
    })
    types_ = [_event_type(event) for event in events]
    assert "error" in types_
    assert "message_delta" not in types_
    assert "message_stop" not in types_


def test_nvidia_anthropic_stream_error_is_terminal():
    compat = _load_nvidia_compat()

    async def upstream():
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        yield ("data: " + json.dumps({
            "error": {"type": "server_error", "message": "synthetic upstream failure"}
        }) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def run():
        return [event async for event in compat.stream_openai_to_anthropic(
            upstream(), "model", {}
        )]

    types_ = [_event_type(event) for event in asyncio.run(run())]
    assert types_[-1] == "error"
    assert "message_delta" not in types_
    assert "message_stop" not in types_
