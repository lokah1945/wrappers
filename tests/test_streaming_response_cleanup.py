"""Exercise transport disconnects, including before stream iteration starts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anyio
import pytest
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

from common.compat import (passthrough_anthropic_sse, translate_anthropic_stream_to_openai_chat,
                           translate_openai_chat_sse_to_responses)
from common.translations import openai_chat_to_anthropic_request, strip_cache_control
from common.sse import ClosingStreamingResponse, IDLE, iter_chunks_with_idle
from common.wrapper_scope import require_production
from test_active_native_passthrough import (
    ROOT, _load, _openrouter_proxy_namespace, _pool, _Upstream, _route_namespace, _request,
)


SCOPE = {'type': 'http', 'asgi': {'spec_version': '2.4'}}


async def never_receive():
    await asyncio.Future()


@pytest.mark.parametrize('stage', ['start', 'body'])
@pytest.mark.parametrize('async_callback', [False, True])
async def test_transport_error_closes_started_iterator_and_always_releases_owner(stage, async_callback):
    events = []

    async def body():
        try:
            yield b'data: future_native_payload\n\n'
            await asyncio.Future()
        finally:
            events.append('iterator_closed')

    def release():
        events.append('released')

    async def release_async():
        await asyncio.sleep(0)
        release()

    response = ClosingStreamingResponse(body(), on_close=release_async if async_callback else release)

    async def send(message):
        if message['type'] == 'http.response.' + stage:
            raise OSError('isolated disconnected transport')

    with pytest.raises(ClientDisconnect):
        await response(SCOPE, never_receive, send)
    assert events == (['released'] if stage == 'start' else ['iterator_closed', 'released'])
    await response.aclose()
    assert events.count('released') == 1


async def test_normal_completion_preserves_wire_status_headers_and_background():
    events, sent = [], []
    wire = [b'event: future\r\ndata: {"unknown":true}\r\n\r\n', b'data: [DONE]']

    async def body():
        for chunk in wire:
            yield chunk

    response = ClosingStreamingResponse(body(), status_code=202, headers={'x-future': 'kept'},
        background=BackgroundTask(events.append, 'background'), on_close=lambda: events.append('released'))

    async def send(message):
        sent.append(message)

    await response(SCOPE, never_receive, send)
    assert sent[0]['status'] == 202 and (b'x-future', b'kept') in sent[0]['headers']
    assert [message['body'] for message in sent[1:]] == wire + [b'']
    assert events == ['background', 'released']


@pytest.mark.parametrize('spec_version', ['2.0', '2.4'])
async def test_cancelled_request_awaits_async_resource_cleanup(spec_version):
    reading, cleaned = asyncio.Event(), asyncio.Event()

    async def body():
        reading.set()
        await asyncio.Future()
        yield b'unreachable'

    async def release():
        await asyncio.sleep(0)
        cleaned.set()

    response = ClosingStreamingResponse(body(), on_close=release)

    async def send(message):
        pass

    task = asyncio.create_task(response({'type': 'http', 'asgi': {'spec_version': spec_version}}, never_receive, send))
    await asyncio.wait_for(reading.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


async def test_anyio_disconnect_cancellation_shields_async_owner_cleanup():
    first_body, cleaned = asyncio.Event(), asyncio.Event()

    async def body():
        yield b'first'
        await asyncio.Future()

    async def receive():
        await first_body.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        if message['type'] == 'http.response.body':
            first_body.set()

    async def release():
        await anyio.sleep(0)
        cleaned.set()

    response = ClosingStreamingResponse(body(), on_close=release)
    await response({'type': 'http', 'asgi': {'spec_version': '2.0'}}, receive, send)
    assert cleaned.is_set()


class HangingUpstream(_Upstream):
    def __init__(self):
        super().__init__()
        self.read_started = asyncio.Event()
        self.read_closed = asyncio.Event()
        self.first_frame = b'event: message_start\ndata: {}\n\n'

    async def _iterate(self):
        try:
            yield self.first_frame
            self.read_started.set()
            await asyncio.Future()
        finally:
            self.read_closed.set()


@pytest.mark.parametrize('stage', ['start', 'body', 'idle'])
@pytest.mark.parametrize('endpoint', ['messages', 'chat/completions', 'responses'])
async def test_actual_openrouter_native_disconnect_releases_peer_key_and_pending_read(stage, endpoint):
    require_production('openrouter')
    pool = _pool('openrouter')
    peer = HangingUpstream()

    async def request(*args, **kwargs):
        return peer

    namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
    namespace['HEARTBEAT_MS'] = 1
    response = await namespace['_proxy_request']('POST', endpoint,
        {'model': 'native/model', 'stream': True}, stream=True, terminal_done=False)

    async def send(message):
        if stage == 'start' and message['type'] == 'http.response.start':
            raise OSError('controlled start failure')
        if message['type'] == 'http.response.body':
            if stage == 'body' or (stage == 'idle' and message['body'].startswith(b': heartbeat')):
                raise OSError('controlled body failure')

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response(SCOPE, never_receive, send), 1)
    assert peer.release_count == 1
    assert all(key.in_flight == 0 for key in pool.keys)
    assert not response._read_tasks
    if stage != 'start':
        assert peer.read_closed.is_set()
    if stage == 'idle':
        assert peer.read_started.is_set()
    await response.aclose()
    assert peer.release_count == 1


@pytest.mark.parametrize('route,anthropic', [
    ('chat_completions', True), ('responses', True), ('responses', False), ('messages', False),
])
@pytest.mark.parametrize('stage', ['start', 'body'])
async def test_actual_openrouter_translated_routes_transfer_pre_acquired_cleanup(route, anthropic, stage):
    require_production('openrouter')
    pool, peer = _pool('openrouter'), HangingUpstream()
    if anthropic:
        peer.first_frame = b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'

    async def request(*args, **kwargs):
        return peer

    proxy_namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
    source = await proxy_namespace['_proxy_request']('POST', 'messages' if anthropic else 'chat/completions',
        {'model': 'native/model', 'stream': True}, stream=True, terminal_done=False)
    namespace = _route_namespace('openrouter', anthropic=anthropic, response=source, captured=[])
    namespace.update(_load(ROOT / 'openrouter/src/main.py', {
        '_translate_openai_stream_to_responses', '_translate_openai_stream_to_anthropic',
        '_remember_response_stream', '_anthropic_to_openai'},
        _iter_chunks_with_idle=iter_chunks_with_idle, _IDLE=IDLE, HEARTBEAT_MS=100000,
        _normalize_sse_newlines=lambda value: value, _strip_cache=strip_cache_control))
    namespace.update({
        'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request,
        '_translate_anthropic_stream_to_openai_chat': translate_anthropic_stream_to_openai_chat,
        '_translate_openai_chat_sse_to_responses': translate_openai_chat_sse_to_responses,
        '_responses_use_native_upstream': lambda: False,
        '_request_principal': lambda request: 'isolated-test',
        'responses_to_chat': lambda body, **kwargs: {'model': body['model'], 'stream': True,
            'max_tokens': 128, 'messages': [{'role': 'user', 'content': 'check'}]},
    })
    body = {'model': 'native/model', 'stream': True, 'max_tokens': 128,
            'messages': [{'role': 'user', 'content': 'check'}], 'input': 'check'}
    response = await namespace[route](_request(body))

    async def send(message):
        if message['type'] == 'http.response.' + stage:
            raise OSError('isolated translated transport failure')

    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response(SCOPE, never_receive, send), 1)
    assert peer.release_count == 1
    assert all(key.in_flight == 0 for key in pool.keys)
    assert not response._read_tasks
    await response.aclose()
    assert peer.release_count == 1


@pytest.mark.parametrize('adapter', ['passthrough', 'translated'])
async def test_nested_anthropic_helpers_close_pending_read_without_gc(adapter):
    closed = asyncio.Event()
    reading = asyncio.Event()

    async def source():
        try:
            if adapter == 'passthrough':
                yield b'event: future\ndata: {}\n\n'
            else:
                yield b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            reading.set()
            await asyncio.Future()
        finally:
            closed.set()

    src = source()
    frames = (passthrough_anthropic_sse(src, .001) if adapter == 'passthrough'
              else translate_anthropic_stream_to_openai_chat(src, 'native/model', .001))
    assert await anext(frames)
    if adapter == 'translated':
        task = asyncio.create_task(anext(frames))
        await asyncio.wait_for(reading.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        while True:
            frame = await asyncio.wait_for(anext(frames), 1)
            if b'heartbeat' in (frame if isinstance(frame, bytes) else frame.encode()):
                break
    await frames.aclose()
    assert closed.is_set()


async def test_response_read_task_ownership_is_isolated_between_requests():
    readers = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    async def source(index):
        try:
            readers[index].set()
            await asyncio.Future()
            yield b'unreachable'
        finally:
            stopped[index].set()

    async def body(index):
        async for chunk in iter_chunks_with_idle(source(index), .001):
            yield b': heartbeat\n\n' if chunk is IDLE else chunk

    async def send(message):
        pass

    responses = [ClosingStreamingResponse(body(index)) for index in range(2)]
    tasks = [asyncio.create_task(response(SCOPE, never_receive, send)) for response in responses]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in readers)), 1)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert stopped[0].is_set() and not stopped[1].is_set()
        assert not tasks[1].done()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert all(event.is_set() for event in stopped)
