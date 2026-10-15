#!/usr/bin/env python3
"""Regression tests: thinking content blocks must carry a signature.

Anthropic contract: every `thinking` block carries a `signature`
(streamed as a `signature_delta` immediately before `content_block_stop`,
present as a field on non-stream blocks). Real Anthropic responses always
include one; strict SDKs and Claude Code interleaved-thinking round-trips
expect a structurally valid block.

Covers: shared think_tags helper, shared AnthropicStreamState,
shared openai_to_anthropic_response, NVIDIA (stream + non-stream),
NOUS (stream state + non-stream via AST load), OpenRouter
(non-stream + stream via AST load), and input-side round-trip acceptance.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "nvidia-python"))
sys.path.insert(0, str(ROOT))

os_env_defaults = {
    "LOG_FILE": "/tmp/wrapper-audit-logs/nvidia.log",
    "EVENTS_FILE": "/tmp/wrapper-audit-logs/e.jsonl",
    "METRICS_DB": "/tmp/wrapper-audit-logs/m.db",
    "VERIFY_ON_BOOT": "false",
    "BEARER_TOKEN": "",
    "FREE_ONLY": "no",
}
import os  # noqa: E402

for k, v in os_env_defaults.items():
    os.environ.setdefault(k, v)

from common.translations.think_tags import (  # noqa: E402
    thinking_signature,
    signature_delta_event,
)
from common.translations import (  # noqa: E402
    AnthropicStreamState as SharedStreamState,
    openai_to_anthropic_response as shared_o2a,
)
from src.anthropic_compat import (  # noqa: E402
    openai_to_anthropic as nv_o2a,
    anthropic_to_openai as nv_a2o,
    stream_openai_to_anthropic as nv_stream,
)
from test_active_native_passthrough import ROOT as _ROOT, _load  # noqa: E402


# --------------------------------------------------------------- helpers
def sse_lines(events):
    """Feed-able OpenAI SSE chunks from dicts."""
    for s in events:
        yield ("data: " + json.dumps(s) + "\n\n").encode()
    yield b"data: [DONE]\n\n"


def parse_anthropic_sse(blob: str):
    """Parse an Anthropic SSE blob into event (type, data) tuples."""
    out = []
    event_type = None
    for line in blob.split("\n"):
        if line.startswith("event: "):
            event_type = line[7:].strip()
        elif line.startswith("data: "):
            try:
                data = json.loads(line[6:])
            except ValueError:
                continue
            out.append((event_type or data.get("type"), data))
    return out


SIG = thinking_signature()


# ------------------------------------------------------------------ tests
def test_signature_helper_is_opaque_stable_token():
    import base64
    tok = thinking_signature()
    assert isinstance(tok, str) and len(tok) >= 20
    # valid base64 (opaque token, not a guessable marker)
    base64.b64decode(tok, validate=True)
    # stable across calls (deterministic)
    assert thinking_signature() == tok
    ev = signature_delta_event(3)
    assert ev == {
        "type": "content_block_delta",
        "index": 3,
        "delta": {"type": "signature_delta", "signature": tok},
    }


def test_shared_stream_state_signs_thinking_block():
    state = SharedStreamState(model="m")
    events = []
    events += state.translate_chunk({"choices": [{"delta": {
        "reasoning_content": "thinking hard"}}]})
    events += state.translate_chunk({"choices": [{"delta": {
        "content": "the answer"}}]})
    events += state.translate_chunk({"choices": [{"delta": {},
        "finish_reason": "stop"}]})
    blob = "".join(events)
    evs = parse_anthropic_sse(blob)
    types = [t for t, _ in evs]
    # signature_delta must appear immediately BEFORE the thinking block stop
    assert "signature_delta" in json.dumps([d for _, d in evs])
    seq = [d.get("delta", {}).get("type") or t for t, d in evs]
    stop_idx = types.index("content_block_stop")
    sig_idx = [i for i, (t, d) in enumerate(evs)
               if t == "content_block_delta"
               and d.get("delta", {}).get("type") == "signature_delta"]
    assert sig_idx, "no signature_delta emitted"
    assert sig_idx[0] < stop_idx, "signature_delta must precede content_block_stop"
    assert seq.count("signature_delta") == 1


def test_shared_nonstream_thinking_signature():
    resp = shared_o2a({
        "id": "chatcmpl-1", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": "answer",
                                 "reasoning_content": "reason"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }, model="m")
    blocks = [b for b in resp["content"] if b.get("type") == "thinking"]
    assert blocks and blocks[0]["signature"] == SIG


def test_nvidia_nonstream_thinking_signature():
    resp = nv_o2a({
        "id": "chatcmpl-2", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant",
                                 "content": "answer",
                                 "reasoning_content": "reason"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }, model="m")
    blocks = [b for b in resp["content"] if b.get("type") == "thinking"]
    assert blocks and blocks[0]["signature"] == SIG


def test_nvidia_stream_signs_thinking_block():
    async def run():
        seq = [
            {"choices": [{"delta": {"reasoning_content": "step one"}}]},
            {"choices": [{"delta": {"content": "final answer"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        out = []
        async for x in nv_stream(sse_lines(seq), "m", {}, expect_thinking=False):
            out.append(x)
        return "".join(out)

    blob = asyncio.run(run())
    evs = parse_anthropic_sse(blob)
    sigs = [d for t, d in evs if t == "content_block_delta"
            and d.get("delta", {}).get("type") == "signature_delta"]
    assert sigs, "NVIDIA stream thinking block emitted without signature"
    assert sigs[0]["delta"]["signature"] == SIG
    # ordering: signature_delta before the first content_block_stop
    stop_i = next(i for i, (t, _) in enumerate(evs) if t == "content_block_stop")
    sig_i = next(i for i, (t, d) in enumerate(evs)
                 if t == "content_block_delta"
                 and d.get("delta", {}).get("type") == "signature_delta")
    assert sig_i < stop_i


def _nous_module():
    return _load(_ROOT / "nous/src/main.py", {
        "AnthropicStreamState", "openai_to_anthropic"})


def test_nous_stream_state_signs_thinking():
    ns = _nous_module()
    state = ns["AnthropicStreamState"]("m")

    def emit(chunks):
        events = []
        for c in chunks:
            events.extend(state.translate_chunk(c))
        events.extend(state.done())
        return events

    # NOUS translate_chunk returns dicts: {"type": event, "data": payload}
    raw = emit([
        {"choices": [{"delta": {"reasoning_content": "reason"}}]},
        {"choices": [{"delta": {"content": "answer"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ])
    evs = [(e["type"], e["data"]) for e in raw if isinstance(e, dict)]
    sigs = [d for t, d in evs if t == "content_block_delta"
            and d.get("delta", {}).get("type") == "signature_delta"]
    assert sigs and sigs[0]["delta"]["signature"] == SIG
    stop_i = next(i for i, (t, _) in enumerate(evs) if t == "content_block_stop")
    sig_i = next(i for i, (t, d) in enumerate(evs)
                 if t == "content_block_delta"
                 and d.get("delta", {}).get("type") == "signature_delta")
    assert sig_i < stop_i, "signature_delta must precede content_block_stop"


def test_nous_nonstream_thinking_signature():
    ns = _nous_module()
    resp = ns["openai_to_anthropic"]("m", {
        "id": "gen-3", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "answer",
                                 "reasoning_content": "reason"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    })
    blocks = [b for b in resp["content"] if b.get("type") == "thinking"]
    assert blocks and blocks[0]["signature"] == SIG


def _or_module():
    from common.sse import IDLE, iter_chunks_with_idle, normalize_sse_newlines
    return _load(_ROOT / "openrouter/src/main.py", {
        "_openai_to_anthropic_response",
        "_translate_openai_stream_to_anthropic",
    }, HEARTBEAT_MS=1, _iter_chunks_with_idle=iter_chunks_with_idle,
        _IDLE=IDLE, _normalize_sse_newlines=normalize_sse_newlines)


def test_or_nonstream_thinking_signature():
    ns = _or_module()
    resp = ns["_openai_to_anthropic_response"]({
        "id": "gen-4", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "answer",
                                 "reasoning_content": "reason"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }, {"model": "m"})
    blocks = [b for b in resp["content"] if b.get("type") == "thinking"]
    assert blocks and blocks[0]["signature"] == SIG


def test_or_stream_signs_thinking():
    ns = _or_module()

    async def openai_gen():
        for s in [
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {"reasoning_content": "reason"}}]},
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {"content": "answer"}}]},
            {"object": "chat.completion.chunk",
             "choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ]:
            yield ("data: " + json.dumps(s) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    async def run():
        out = []
        async for x in ns["_translate_openai_stream_to_anthropic"](
                openai_gen(), {"model": "m"}):
            out.append(x)
        return "".join(out)

    blob = asyncio.run(run())
    parsed = parse_anthropic_sse(blob)
    sigs = [d for t, d in parsed if t == "content_block_delta"
            and d.get("delta", {}).get("type") == "signature_delta"]
    assert sigs and sigs[0]["delta"]["signature"] == SIG


def test_input_thinking_block_roundtrip_accepted():
    """Claude Code passes thinking blocks back verbatim on tool continuation —
    the wrapper must accept them (with signature) on the request side."""
    body = {
        "model": "m",
        "max_tokens": 100,
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "prior reasoning",
                 "signature": SIG},
                {"type": "text", "text": "partial"},
            ]},
            {"role": "user", "content": "continue"},
        ],
    }
    oai = nv_a2o(body)
    msgs = oai["messages"]
    assert len(msgs) >= 2
    # Prior thinking must not crash the translation nor leak as VISIBLE text;
    # carrying it in reasoning_content is transparent, correct passthrough
    # (upstream reasoning models accept assistant reasoning back).
    for m in msgs:
        if m.get("role") == "assistant":
            visible = m.get("content")
            if isinstance(visible, str):
                assert "prior reasoning" not in visible
            elif isinstance(visible, list):
                for part in visible:
                    if isinstance(part, dict) and part.get("type") == "text":
                        assert "prior reasoning" not in part.get("text", "")
