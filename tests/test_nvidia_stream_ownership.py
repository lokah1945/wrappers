"""NVIDIA pre-start and nested streaming ownership with synthetic upstreams."""
from __future__ import annotations

import ast
import asyncio
import json
from types import MethodType

import pytest
from starlette.requests import ClientDisconnect

from common.sse import ClosingStreamingResponse
from common.compat import (translate_anthropic_stream_to_openai_chat,
                           translate_openai_chat_sse_to_responses)
from common.translations import (
    anthropic_to_openai_response,
    openai_chat_to_anthropic_request,
)
from test_nvidia_messages_request_controls import (
    MODEL, ROOT, SOURCE, Peer, _module, _request, _server,
)

RESPONSES = _module('nvidia_controls_fixture.responses_compat',
                    ROOT / 'nvidia-python/src/responses_compat.py',
                    package='nvidia_controls_fixture')

WIRE = (b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
        b'data: [DONE]\n\n')
ANTHROPIC_WIRE = (b'event: message_start\ndata: {"type":"message_start",'
                  b'"message":{"id":"msg_mock","model":"exact/model",'
                  b'"role":"assistant","content":[],"usage":{"input_tokens":1}}}\n\n'
                  b'event: message_stop\ndata: {"type":"message_stop"}\n\n')


def server_for(peer, *, native=False):
    records = []
    server, namespace = _server([peer], records, native=native)
    namespace.update({
        '__package__': 'nvidia_controls_fixture',
        'StreamingResponse': ClosingStreamingResponse,
        'openai_chat_to_anthropic_request': openai_chat_to_anthropic_request,
        'anthropic_to_openai_response': anthropic_to_openai_response,
        '_translate_anthropic_stream_to_openai_chat': translate_anthropic_stream_to_openai_chat,
        '_translate_openai_chat_sse_to_responses': translate_openai_chat_sse_to_responses,
    })
    names = {'_handle_chat_completions', '_stream_chat', '_stream_proxy',
             '_compat2_chat', '_compat2_messages', '_compat2_responses'}
    definition = next(node for node in ast.parse(SOURCE.read_text()).body
                      if isinstance(node, ast.ClassDef) and node.name == 'Server')
    methods = [node for node in definition.body if getattr(node, 'name', None) in names]
    future = ast.ImportFrom(module='__future__',
                            names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *methods],
                                                     type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    for name in names:
        setattr(server, name, MethodType(namespace[name], server))
    return server, records


async def make_response(server, surface):
    body = {'model': MODEL, 'max_tokens': 100, 'stream': True,
            'messages': [{'role': 'user', 'content': 'Synthetic input.'}]}
    request = _request(body)
    if surface == 'chat':
        return await server._handle_chat_completions(body, request, b'')
    if surface == 'messages':
        return await server._handle_anthropic_messages(json.dumps(body).encode(), request)
    if surface == 'native_responses':
        return await server._handle_catch_all(request, '/v1/responses')
    if surface == 'post':
        return await server._proxy_post(request, body, b'', MODEL, '/v1/completions',
                                       lambda key: 'http://synthetic.invalid/v1/completions')
    if surface == 'anthropic_chat':
        return await server._compat2_chat(body, request)
    if surface == 'anthropic_messages':
        return await server._compat2_messages(body, request)
    response_body = {'model': MODEL, 'input': 'Synthetic input.', 'stream': True,
                     'max_output_tokens': 100}
    if surface == 'anthropic_responses':
        return await server._compat2_responses(response_body, request)
    assert surface == 'responses_adapter'
    handler = RESPONSES.ResponsesHandler({
        'pool': server.pool, 'resolve_target_model': lambda value: value,
        'proxy_openai': server.proxy_openai, 'forward_headers': lambda request: {},
    })
    result, stream = await handler.translate_to_nim(request, response_body, MODEL)
    assert result is None
    return ClosingStreamingResponse(stream, media_type='text/event-stream')


async def fail_transport(response, *, event, cancelled=False, spec_version='2.4'):
    interrupted = False
    async def receive():
        await asyncio.Future()

    async def send(message):
        nonlocal interrupted
        if message['type'] == event:
            interrupted = True
            if cancelled:
                raise asyncio.CancelledError()
            raise OSError('synthetic disconnected client')

    try:
        await response({'type': 'http', 'asgi': {'spec_version': spec_version}}, receive, send)
    except BaseException as error:
        errors = list(getattr(error, 'exceptions', (error,)))
        while errors:
            failure = errors.pop()
            nested = getattr(failure, 'exceptions', None)
            if nested:
                errors.extend(nested)
            else:
                expected = (asyncio.CancelledError,) if cancelled else (OSError, ClientDisconnect)
                assert isinstance(failure, expected), repr(failure)
    else:
        # Starlette's pre-2.4 task group absorbs a child-task cancellation.
        assert cancelled and spec_version == '2.3'
    assert interrupted, 'the response failed before the intended transport interruption'


@pytest.mark.parametrize('surface', [
    'chat', 'messages', 'native_responses', 'post', 'anthropic_chat',
    'anthropic_messages', 'anthropic_responses', 'responses_adapter',
])
@pytest.mark.parametrize('event,cancelled', [
    ('http.response.start', False), ('http.response.start', True),
    ('http.response.body', False), ('http.response.body', True),
])
@pytest.mark.parametrize('spec_version', ['2.3', '2.4'])
async def test_transport_failure_releases_pre_acquired_stream_once(surface, event, cancelled, spec_version):
    native = surface.startswith('anthropic_')
    peer = Peer([ANTHROPIC_WIRE if native else WIRE])
    server, records = server_for(peer, native=native)
    response = await make_response(server, surface)
    assert server._in_flight == server.pool.keys[0].in_flight == 1
    assert len(records) == 1
    assert records[0][1]['json']['model'] == MODEL
    await fail_transport(response, event=event, cancelled=cancelled, spec_version=spec_version)
    assert peer.release_count == 1
    assert server._in_flight == server.pool.keys[0].in_flight == 0
    await response.aclose()
    assert peer.release_count == 1


async def test_responses_adapter_manual_close_after_synthetic_event_releases_upstream():
    peer = Peer([WIRE])
    server, _records = server_for(peer)
    response = await make_response(server, 'responses_adapter')
    frame = await response.body_iterator.__anext__()
    assert 'response.created' in frame
    # The adapter has not entered the upstream read loop at this point.
    assert peer.release_count == 0
    await response.body_iterator.aclose()
    assert peer.release_count == 1
    assert server._in_flight == server.pool.keys[0].in_flight == 0


@pytest.mark.parametrize('surface', ['anthropic_chat', 'anthropic_messages',
                                    'anthropic_responses'])
async def test_nested_compatibility_iterator_closed_before_response_close_returns(surface):
    peer = Peer([ANTHROPIC_WIRE])
    server, _records = server_for(peer, native=True)
    state = {'started': False, 'closed': False}

    async def child(*args, **kwargs):
        state['started'] = True
        try:
            yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
            await asyncio.Future()
        finally:
            state['closed'] = True

    namespace = server._compat2_chat.__func__.__globals__
    namespace['_passthrough_anthropic_sse' if surface == 'anthropic_messages'
              else '_translate_anthropic_stream_to_openai_chat'] = child
    response = await make_response(server, surface)
    # Responses emits synthetic events before the child begins. Stop at its
    # first translated body frame, while that child is still suspended.
    for _ in range(20):
        await response.body_iterator.__anext__()
        if state['started']:
            break
    assert state['started'] and not state['closed']
    await response.aclose()
    assert state['closed'], 'child iterator cleanup must complete synchronously'
    assert peer.release_count == 1
    assert server._in_flight == server.pool.keys[0].in_flight == 0


@pytest.mark.parametrize('surface', ['chat', 'messages', 'native_responses',
                                    'post', 'responses_adapter'])
async def test_successful_eof_releases_once_and_preserves_terminal_event(surface):
    peer = Peer([WIRE])
    server, _records = server_for(peer)
    response = await make_response(server, surface)
    sent = []

    async def receive():
        await asyncio.Future()

    async def send(message):
        sent.append(message)

    await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
    body = b''.join(message.get('body', b'') for message in sent)
    assert (b'message_stop' if surface == 'messages' else b'[DONE]') in body
    assert peer.release_count == 1
    assert server._in_flight == server.pool.keys[0].in_flight == 0
