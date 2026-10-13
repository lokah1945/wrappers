"""Real Nous stream routes release pre-acquired keys on ASGI transport failure."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from starlette.requests import ClientDisconnect

from common.compat import (
    translate_anthropic_stream_to_openai_chat,
    translate_openai_chat_sse_to_responses,
)
from common.wrapper_scope import production_wrappers
from test_active_native_passthrough import ROOT, _Upstream, _load, _request, _route_namespace

assert 'nous' in production_wrappers(include_registry=False)
MODES = ['chat_native', 'chat_cross', 'responses_native', 'responses_chat',
         'responses_cross', 'messages_native', 'messages_cross']


def route_fixture(mode):
    upstream = _Upstream([b': fixture\n\n'])
    key = object()
    releases = []

    async def release(entry):
        if entry is not None:
            releases.append(entry)

    async def proxy(*args, **kwargs):
        return 200, upstream, key

    async def store(*args, **kwargs):
        pass

    namespace = _route_namespace('nous', anthropic=mode in {
        'chat_cross', 'responses_cross', 'messages_native'}, response=upstream, captured=[])
    namespace.update(_load(ROOT / 'nous/src/main.py', {
        'ResponsesStreamState', 'AnthropicStreamState', '_responses_sse_serialize',
        'stream_with_heartbeat', '_remember_compat_responses_stream'},
        **{k: v for k, v in namespace.items() if k not in {
            'asyncio', 'json', 'os', 'random', 'time', 'logger', 'aiohttp',
            'JSONResponse', 'Response', 'StreamingResponse', 'close_stream'}}))
    namespace.update({
        'post_nous_with_retries': proxy, 'KEY_POOL': SimpleNamespace(release=release),
        '_responses_use_native_upstream': lambda: mode == 'responses_native',
        '_translate_anthropic_stream_to_openai_chat': translate_anthropic_stream_to_openai_chat,
        '_translate_openai_chat_sse_to_responses': translate_openai_chat_sse_to_responses,
        'openai_chat_to_anthropic_request': lambda body: dict(body),
        'anthropic_to_openai': lambda body: dict(body),
        '_validate_anthropic_chat_request': lambda body: None,
        'responses_to_chat': lambda body, principal: dict(body),
        '_extract_principal': lambda request: 'fixture-principal',
        'store_conversation': store, 'HEARTBEAT_MS': 1,
    })
    # AST functions share their execution globals. Propagate dependencies into
    # each namespace so these are the actual production functions, not copies
    # of request/cleanup behavior.
    for value in list(namespace.values()):
        if callable(value) and getattr(value, '__module__', None) is None and hasattr(value, '__globals__'):
            value.__globals__.update(namespace)
    for name in ('chat_completions', 'responses', 'messages', 'stream_with_heartbeat',
                 '_remember_compat_responses_stream'):
        namespace[name].__globals__.update(namespace)
    return namespace, upstream, key, releases


@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('failure_at', ['start', 'body'])
async def test_transport_failure_releases_nous_stream_and_key_once(mode, failure_at):
    namespace, upstream, key, releases = route_fixture(mode)
    surface = mode.split('_', 1)[0]
    route = 'chat_completions' if surface == 'chat' else surface
    response = await namespace[route](_request({
        'model': 'exact/model', 'stream': True, 'max_tokens': 128,
        'messages': [{'role': 'user', 'content': 'fixture'}],
    }))
    assert releases == [] and upstream.release_count == 0

    async def receive():
        await asyncio.Future()

    failed_sends = []

    async def send(message):
        if message['type'] == 'http.response.' + failure_at:
            failed_sends.append(message['type'])
            raise OSError('fixture client disconnected')

    with pytest.raises((OSError, ClientDisconnect)):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    await response.aclose()
    assert failed_sends == ['http.response.' + failure_at]
    assert upstream.release_count == 1
    assert releases == [key]


@pytest.mark.parametrize('surface', ['chat', 'responses', 'messages'])
async def test_normal_native_stream_preserves_bytes_and_releases_once(surface):
    namespace, upstream, key, releases = route_fixture(surface + '_native')
    frame = b'event: unknown_future_event\ndata: {"future":true}\n\n'
    upstream.chunks = [frame]
    route = 'chat_completions' if surface == 'chat' else surface
    response = await namespace[route](_request({
        'model': 'exact/model', 'stream': True, 'max_tokens': 128,
        'messages': [{'role': 'user', 'content': 'fixture'}],
    }))
    frames = [chunk async for chunk in response.body_iterator]
    await response.aclose()
    assert frames == [frame]
    assert upstream.release_count == 1 and releases == [key]


async def test_key_release_is_shielded_when_generator_cleanup_is_cancelled():
    releases = []
    upstream = _Upstream()
    key = object()

    async def release(entry):
        # Pool release acquires an async lock. This checkpoint represents
        # contention, where an already cancelled ASGI scope must not skip it.
        await anyio.sleep(0)
        releases.append(entry)

    namespace = _load(ROOT / 'nous/src/main.py', {'_stream_resource_cleanup'},
                      KEY_POOL=SimpleNamespace(release=release))
    cleanup = namespace['_stream_resource_cleanup'](upstream, key)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await cleanup()
    await cleanup()
    assert releases == [key] and upstream.release_count == 1
