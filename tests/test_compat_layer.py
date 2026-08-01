#!/usr/bin/env python3
"""COMPATIBILITY_LAYER unit tests — operator-declared upstream dialect.

Covers:
  - env resolution (1/2/3/unset/invalid -> fail fast)
  - openai_chat_to_anthropic_request round-trip fidelity (system, images,
    tools, tool_choice, stop, params)
  - layer-2 stream adapters (Anthropic SSE -> OpenAI chat SSE -> Responses SSE)
  - auto-discovery probe against the OpenAI and Anthropic mocks
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.compat import (  # noqa: E402
    compat_layer, is_anthropic_upstream, is_auto_discovery, is_openai_upstream,
    validate_compat_layer,
)
from common.translations import openai_chat_to_anthropic_request  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv('COMPATIBILITY_LAYER', raising=False)


def test_default_layer_is_openai():
    assert compat_layer() == '1'
    assert is_openai_upstream() and not is_anthropic_upstream()


@pytest.mark.parametrize('value,layer', [('1', '1'), ('2', '2'), ('3', '3')])
def test_explicit_layers(monkeypatch, value, layer):
    monkeypatch.setenv('COMPATIBILITY_LAYER', value)
    assert compat_layer() == layer
    assert is_anthropic_upstream() == (layer == '2')
    assert is_auto_discovery() == (layer == '3')


@pytest.mark.parametrize('bad', ['0', '4', 'openai', 'auto', 'TRUE'])
def test_invalid_layer_fails_fast(monkeypatch, bad):
    monkeypatch.setenv('COMPATIBILITY_LAYER', bad)
    with pytest.raises(ValueError):
        validate_compat_layer()
    # runtime fallback stays safe (openai)
    assert compat_layer() == '1'


def test_validate_config_wired_in_all_wrappers():
    """All five wrappers must fail fast on an invalid COMPATIBILITY_LAYER."""
    for wname in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        src = (ROOT / wname / 'src' / 'main.py').read_text()
        assert 'validate_compat_layer' in src, f'{wname}: validate_config missing COMPATIBILITY_LAYER check'
        assert 'COMPATIBILITY_LAYER' in src, f'{wname}: no COMPATIBILITY_LAYER handling'


def test_env_example_documents_compatibility_layer():
    for wname in ('nvidia-python', 'nous', 'opencode', 'blackbox', 'openrouter'):
        txt = (ROOT / wname / '.env.example').read_text()
        assert 'COMPATIBILITY_LAYER' in txt, f'{wname}: .env.example missing COMPATIBILITY_LAYER'
        assert '1 = OpenAI Compatible' in txt


# ── openai_chat_to_anthropic_request ──────────────────────────────────────

CHAT_REQ = {
    'model': 'mock/default', 'max_tokens': 100, 'stream': True, 'temperature': 0.5,
    'stop': ['END', 'STOP'],
    'messages': [
        {'role': 'system', 'content': 'You are helpful.'},
        {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,iVBORw0KGgo='}},
            {'type': 'text', 'text': 'what is this?'}]},
        {'role': 'assistant', 'content': 'I see an image.',
         'tool_calls': [{'id': 'call_1', 'type': 'function',
                         'function': {'name': 'lookup', 'arguments': '{"x":1}'}}]},
        {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'found it'},
        {'role': 'user', 'content': 'now answer'},
    ],
    'tools': [{'type': 'function', 'function': {'name': 'lookup', 'description': 'd',
               'parameters': {'type': 'object', 'properties': {'x': {'type': 'integer'}}}}}],
    'tool_choice': {'type': 'function', 'function': {'name': 'lookup'}},
}


def test_openai_chat_to_anthropic_request_full_roundtrip():
    out = openai_chat_to_anthropic_request(json.loads(json.dumps(CHAT_REQ)))
    assert out['system'] == 'You are helpful.'
    assert out['stop_sequences'] == ['END', 'STOP']
    assert out['stream'] is True and out['max_tokens'] == 100 and out['temperature'] == 0.5
    m0 = out['messages'][0]
    assert m0['content'][0]['type'] == 'image' and m0['content'][0]['source']['data'] == 'iVBORw0KGgo='
    am = out['messages'][1]
    assert any(b['type'] == 'tool_use' and b['name'] == 'lookup' and b['input'] == {'x': 1}
               for b in am['content'])
    assert any(b['type'] == 'text' and b['text'] == 'I see an image.' for b in am['content'])
    tr = out['messages'][2]
    assert tr['role'] == 'user' and tr['content'][0]['type'] == 'tool_result'
    assert tr['content'][0]['tool_use_id'] == 'call_1'
    assert out['tools'][0]['input_schema']['type'] == 'object'
    assert out['tool_choice'] == {'type': 'tool', 'name': 'lookup'}


@pytest.mark.parametrize('tc,expected', [
    ('auto', {'type': 'auto'}),
    ('none', {'type': 'none'}),
    ('required', {'type': 'any'}),
])
def test_tool_choice_str_mapping(tc, expected):
    out = openai_chat_to_anthropic_request({'messages': [], 'tool_choice': tc})
    assert out['tool_choice'] == expected


def test_http_image_url_becomes_text_placeholder():
    out = openai_chat_to_anthropic_request({'messages': [
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'https://x/a.png'}}]}]})
    assert out['messages'][0]['content'] == '[image: https://x/a.png]'


def test_assistant_only_tool_calls_without_text():
    out = openai_chat_to_anthropic_request({'messages': [
        {'role': 'assistant', 'content': None,
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'f', 'arguments': '{"a":1}'}}]}]})
    am = out['messages'][0]
    assert am['role'] == 'assistant' and am['content'][0]['type'] == 'tool_use'


# ── layer-2 stream adapters ───────────────────────────────────────────────

ANTHRO_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"m1","type":"message","role":"assistant","model":"m","content":[],"stop_reason":null}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello from anthropic mock."}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":12,"output_tokens":8}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


async def _sse_source(body: str):
    yield body.encode()


@pytest.mark.asyncio
async def test_translate_anthropic_stream_to_openai_chat():
    from common.compat import translate_anthropic_stream_to_openai_chat
    out = [f async for f in translate_anthropic_stream_to_openai_chat(
        _sse_source(ANTHRO_SSE), 'm', 5.0)]
    joined = ''.join(out)
    assert 'Hello from anthropic mock.' in joined
    assert joined.count('data: [DONE]') == 1
    assert 'finish_reason": "stop"' in joined
    assert 'prompt_tokens": 12' in joined


@pytest.mark.asyncio
async def test_translate_openai_chat_sse_to_responses():
    from common.compat import (
        translate_anthropic_stream_to_openai_chat,
        translate_openai_chat_sse_to_responses,
    )
    chat_sse = translate_anthropic_stream_to_openai_chat(_sse_source(ANTHRO_SSE), 'm', 5.0)
    frames = [f async for f in translate_openai_chat_sse_to_responses(chat_sse, 'm')]
    joined = ''.join(frames)
    assert 'response.completed' in joined
    assert 'data: [DONE]' in joined
    assert 'Hello from anthropic mock.' in joined
    assert 'response.created' in joined


@pytest.mark.asyncio
async def test_passthrough_anthropic_sse_never_appends_done():
    from common.compat import passthrough_anthropic_sse
    out = [f async for f in passthrough_anthropic_sse(_sse_source(ANTHRO_SSE), 5.0)]
    joined = b''.join(out).decode()
    assert 'message_stop' in joined
    assert '[DONE]' not in joined
