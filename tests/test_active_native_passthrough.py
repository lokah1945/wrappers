"""Native protocol contracts without .env files, wrapper startup, or live calls.

These tests execute the production routes and streaming helpers through their
ASTs, using only active providers. Native requests and replies must retain
fields that a newer client/upstream may introduce.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import itertools
import logging
import os
import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from starlette.responses import JSONResponse, Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.compat import passthrough_anthropic_sse, responses_chat_fallback_allowed
from common.model import classify_upstream_error
from common.sse import IDLE, iter_chunks_with_idle, ClosingStreamingResponse as StreamingResponse, close_stream
from common.translations import (
    anthropic_to_openai_response, is_retriable_status, parse_retry_after,
    should_cooldown_key, strip_cache_control, normalize_upstream_error,
)
from common.translations.think_tags import (
    ThinkTagStreamFilter, extract_inline_thinking, strip_trailing_think_tags,
)
from common.translations import parse_dsml_from_text


def _load(path, names, **globals_):
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if getattr(node, 'name', None) in names]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    namespace = dict(
        asyncio=asyncio, json=json, os=os, random=random, time=time,
        logger=logging.getLogger(__name__), aiohttp=aiohttp,
        JSONResponse=JSONResponse, Response=Response,
        StreamingResponse=StreamingResponse, close_stream=close_stream,
    )
    # Anti-trace helpers: setdefault so callers that re-pass a previously
    # built namespace (as **globals_) never hit duplicate keywords, and
    # explicit caller overrides always win.
    namespace.setdefault('_ThinkTagStreamFilter', ThinkTagStreamFilter)
    namespace.setdefault('_extract_inline_thinking', extract_inline_thinking)
    namespace.setdefault('_strip_think_tags', strip_trailing_think_tags)
    namespace.setdefault('_parse_dsml_from_text', parse_dsml_from_text)
    namespace.update(globals_)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace


class _Upstream:
    def __init__(self, chunks=(), *, status=200, reply=None):
        self.chunks = list(chunks)
        self.status = status
        self.reply = reply
        self.headers = {'content-type': 'application/json' if reply is not None else 'text/event-stream',
                        'x-upstream-marker': 'retained'}
        self.content = self
        self.release_count = 0

    async def _iterate(self):
        for chunk in self.chunks:
            yield chunk

    def __aiter__(self):
        return self._iterate()

    def iter_any(self):
        return self._iterate()

    async def text(self):
        return json.dumps(self.reply)

    def release(self):
        self.release_count += 1

    def __await__(self):
        async def complete():
            return self
        return complete().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.release()


async def _collect(response):
    return b''.join([chunk if isinstance(chunk, bytes) else chunk.encode()
                    async for chunk in response.body_iterator])


def _request(body):
    async def body_json():
        return copy.deepcopy(body)
    return SimpleNamespace(json=body_json, headers={}, client=None)


def _route_namespace(wrapper, *, anthropic, response, captured, status=200):
    def no_conversion(*args, **kwargs):
        pytest.fail('native matching route called a translator')

    async def authorized(request):
        pass

    async def proxy(*args, **kwargs):
        captured.append((args, kwargs))
        if wrapper == 'nous':
            return status, response, None
        return response

    async def record(*args, **kwargs):
        pass

    def discard_background(coro, *args):
        coro.close()

    async def release(key):
        pass

    globals_ = {
        '_auth_check': authorized, 'check_rate_limit': lambda *args: True,
        'validate_max_tokens_field': lambda *args, **kwargs: None,
        'resolve_model': lambda model: model,
        'free_only_enabled': lambda: False, '_check_free_only': lambda model: None,
        '_is_anthropic_upstream': lambda: anthropic,
        '_responses_use_native_upstream': lambda: not anthropic,
        '_responses_chat_fallback_allowed': responses_chat_fallback_allowed,
        '_build_forward_headers': lambda headers: dict(headers),
        'metrics': SimpleNamespace(record=lambda **kwargs: None,
                                   record_stream_usage=lambda **kwargs: None),
        'record_model_result': record, '_fire_and_forget': discard_background,
        'post_nous_with_retries': proxy, '_proxy_request': proxy,
        'anthropic_to_openai_response': no_conversion,
        'openai_chat_to_anthropic_request': no_conversion,
        'anthropic_to_openai': no_conversion, '_anthropic_to_openai': no_conversion,
        '_ensure_chat_content': no_conversion,
        'responses_to_chat': no_conversion,
        '_extract_principal': no_conversion,
        '_request_principal': no_conversion,
        '_sanitize_header_value': lambda value: value,
        '_passthrough_anthropic_sse': passthrough_anthropic_sse,
        'HEARTBEAT_MS': 100000,
        'KEY_POOL': SimpleNamespace(release=release),
        '_error_response': lambda content, status_code=500, **kwargs:
            JSONResponse(content, status_code=status_code),
    }
    names = {'chat_completions', 'messages', 'responses'}
    if wrapper == 'nous':
        names.update({'_stream_passthrough', '_stream_resource_cleanup', '_native_json_response', '_native_response_headers'})
        # DASH fix: the routes now record per-model/key/activity metrics via
        # these module helpers — extract them with the routes so the sandbox
        # has the same dependencies as production.
        names.update({'_result_usage_tokens', '_metrics_key_label', '_sniff_stream_usage'})
    return _load(ROOT / wrapper / 'src/main.py', names, **globals_)


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('anthropic', [False, True])
async def test_native_json_preserves_unknown_fields_builtin_tools_and_null_content(wrapper, anthropic):
    body = {
        'model': 'native/model', 'max_tokens': 100,
        'messages': [{'role': 'future_native_role', 'content': [{
            'type': 'future_native_block', 'payload': {'value': 'new'}}]}],
        'tools': [{'type': 'web_search_20990101', 'name': 'web_search', 'future_setting': True}],
        'future_parameter': {'nested': [1, None]},
    }
    if anthropic:
        body['system'] = [{'type': 'text', 'text': 'Keep my cache annotation',
                           'cache_control': {'type': 'ephemeral'}}]
        reply = {'id': 'upstream-id', 'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'future_native_output', 'raw': None}],
                 'model': 'native/model', 'future_metadata': [1, 2]}
    else:
        reply = {'id': 'upstream-id', 'object': 'chat.completion',
                 'choices': [{'message': {'role': 'assistant', 'content': None,
                                         'future_output': {'x': True}}}],
                 'model': 'native/model', 'future_metadata': [1, 2]}
    original = copy.deepcopy(reply)
    captured = []
    response = reply if wrapper == 'nous' else JSONResponse(reply)
    namespace = _route_namespace(wrapper, anthropic=anthropic, response=response, captured=captured)
    result = await namespace['messages' if anthropic else 'chat_completions'](_request(body))
    assert result.status_code == 200
    assert json.loads(result.body) == original
    assert captured[0][0][0 if wrapper == 'nous' else 2] == body
    assert reply == original


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
async def test_declared_openai_route_does_not_guess_and_translate_reply(wrapper):
    reply = {'id': 'upstream', 'type': 'message', 'content': [{'type': 'text', 'text': 'raw'}]}
    captured = []
    namespace = _route_namespace(wrapper, anthropic=False,
                                 response=reply if wrapper == 'nous' else JSONResponse(reply),
                                 captured=captured)
    result = await namespace['chat_completions'](_request({
        'model': 'native/model', 'messages': [{'role': 'user', 'content': 'hi'}]}))
    assert json.loads(result.body) == reply


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('anthropic', [False, True])
async def test_native_routes_preserve_sse_metadata_unknown_events_and_exact_bytes(wrapper, anthropic):
    wire = (b': upstream-comment\r\nid: future-id\r\nretry: 912\r\n'
            b'event: future_native_event\r\ndata: {"future":true,\r\ndata: "value":1}\r\n\r\n'
            b'data: {"content":"literal [DONE] text"}\n\n'
            b'event: terminal_v99\ndata: {"terminal":true}\n\n')
    chunks = [wire[index:index + 1] for index in range(len(wire))]
    captured = []
    upstream = _Upstream(chunks)
    response = upstream if wrapper == 'nous' else StreamingResponse(upstream)
    namespace = _route_namespace(wrapper, anthropic=anthropic, response=response, captured=captured)
    body = {'model': 'native/model', 'max_tokens': 100, 'stream': True,
            'messages': [{'role': 'user', 'content': 'hi'}], 'future_parameter': True}
    result = await namespace['messages' if anthropic else 'chat_completions'](_request(body))
    assert await _collect(result) == wire
    assert captured[0][0][0 if wrapper == 'nous' else 2] == body
    if wrapper == 'nous':
        assert upstream.release_count == 1


async def test_nous_native_stream_close_releases_upstream_and_key_once():
    released_keys = []

    async def release(key):
        released_keys.append(key)

    namespace = _load(ROOT / 'nous/src/main.py', {'_stream_passthrough', '_stream_resource_cleanup'},
                      _passthrough_anthropic_sse=passthrough_anthropic_sse,
                      HEARTBEAT_MS=100000, KEY_POOL=SimpleNamespace(release=release))
    upstream = _Upstream([b'data: first\n\n', b'data: second\n\n'])
    stream = namespace['_stream_passthrough'](upstream, 'test-key')
    assert await anext(stream) == b'data: first\n\n'
    await stream.aclose()
    assert upstream.release_count == 1
    assert released_keys == ['test-key']


async def test_idle_heartbeat_never_splits_event_type_data_or_crlf(monkeypatch):
    import common.compat as compat
    import common.sse as sse

    pieces = [b'event: future\r', b'\ndata: {"x":1}\r\n', b'\r', b'\n']

    async def split_source(src, idle_sec):
        for piece in pieces:
            yield piece
            yield IDLE

    ticks = itertools.count()
    monkeypatch.setattr(compat, 'time', SimpleNamespace(time=lambda: next(ticks)))
    monkeypatch.setattr(sse, 'iter_chunks_with_idle', split_source)
    output = [chunk async for chunk in compat.passthrough_anthropic_sse(None, 0.5)]
    assert output == pieces + [b': heartbeat\n\n']


def _pool(wrapper):
    path = ROOT / wrapper / 'src/main.py' if wrapper == 'nous' else ROOT / wrapper / 'src/key_pool.py'
    namespace = _load(path, {'KeyEntry', 'KeyPool'})
    pool = namespace['KeyPool']()
    pool.keys = [namespace['KeyEntry']('first', 'mock-exhausted-key'),
                 namespace['KeyEntry']('second', 'mock-working-key')]
    return pool


def _registry():
    return SimpleNamespace(call_plan=lambda model, surface: SimpleNamespace(
        model=SimpleNamespace(provider_model_id=model)))


async def test_nous_cancellation_in_retry_jitter_releases_reserved_key(monkeypatch):
    monkeypatch.setenv('RETRY_BACKOFF_BASE_SEC', '60')
    pool = _pool('nous')
    second_acquired = asyncio.Event()
    original_acquire = pool.acquire

    async def acquire(*args, **kwargs):
        entry = await original_acquire(*args, **kwargs)
        if entry and entry.label == 'second':
            second_acquired.set()
        return entry

    pool.acquire = acquire

    async def post(*args, **kwargs):
        return 429, {'error': {'message': 'rate limited', 'retry_after': 60}}

    namespace = _load(ROOT / 'nous/src/main.py', {
        'post_nous_with_retries', '_retry_after_seconds',
        '_is_retriable_upstream_status', '_looks_model_capacity_error'},
        MODEL_REGISTRY=_registry(), same_provider_model_id=lambda *args: True,
        KEY_POOL=pool, _read_token_from_auth_path=lambda: None,
        post_nous=post, _parse_retry_after=parse_retry_after,
        _should_cooldown_key=should_cooldown_key,
        classify_upstream_error=classify_upstream_error)
    task = asyncio.create_task(namespace['post_nous_with_retries']({'model': 'native/model'}))
    await asyncio.wait_for(second_acquired.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(key.in_flight == 0 for key in pool.keys)


def _openrouter_proxy_namespace(pool, agent):
    async def get_agent():
        return agent

    async def record(*args, **kwargs):
        pass

    def discard_background(coro, *args):
        coro.close()

    return _load(ROOT / 'openrouter/src/main.py',
                 {'_proxy_request', '_usage_tokens', '_sniff_stream_usage'},
                 MODEL_REGISTRY=_registry(), same_provider_model_id=lambda *args: True,
                 pool=pool, OPENROUTER_BASE='http://mock.invalid/v1', get_agent=get_agent,
                 _build_forward_headers=lambda headers: dict(headers),
                 _parse_retry_after=parse_retry_after, _is_retriable_status=is_retriable_status,
                 _should_cooldown_key=should_cooldown_key,
                 classify_upstream_error=classify_upstream_error,
                 _responses_chat_fallback_allowed=responses_chat_fallback_allowed,
                 _spawn_background=discard_background, _record_model_result=record,
                 metrics=SimpleNamespace(record_request=record, record_tokens=record),
                 sanitize_header_value=lambda value: value,
                 _iter_chunks_with_idle=iter_chunks_with_idle, _IDLE=IDLE, HEARTBEAT_MS=100000,
                 _error_response=lambda content, status_code=500, **kwargs:
                    JSONResponse(content, status_code=status_code))


@pytest.mark.parametrize('wire', [b'data: [DONE]', b'event: future\ndata: {"x":1}\n\n'])
async def test_openrouter_proxy_native_stream_has_no_synthetic_terminal(wire):
    pool = _pool('openrouter')
    upstream = _Upstream([wire[index:index + 1] for index in range(len(wire))])

    async def request(*args, **kwargs):
        return upstream

    namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
    response = await namespace['_proxy_request']('POST', 'chat/completions',
                                               {'model': 'native/model', 'stream': True}, stream=True)
    assert await _collect(response) == wire
    assert response.headers['x-upstream-marker'] == 'retained'
    assert upstream.release_count == 1
    assert all(key.in_flight == 0 for key in pool.keys)


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('status', [429, 401, 402, 503])
async def test_hot_swap_retries_same_payload_and_model_with_next_credential(wrapper, status, monkeypatch):
    monkeypatch.setenv('RETRY_BACKOFF_BASE_SEC', '0')
    pool = _pool(wrapper)
    payload = {'model': 'native/model', 'messages': [{'role': 'user', 'content': 'same request'}],
               'future_parameter': {'same': True}}
    before = copy.deepcopy(payload)
    calls = []
    success = {'id': 'success', 'choices': [{'message': {'content': 'ok'}}]}
    failure = {'error': {'type': 'rate_limit_error' if status == 429 else 'api_error',
                         'message': 'mock credential exhausted', 'retry_after': 60}}
    if wrapper == 'nous':
        async def post(body, token, **kwargs):
            calls.append((token, copy.deepcopy(body)))
            return (status, failure) if token == 'mock-exhausted-key' else (200, success)

        namespace = _load(ROOT / 'nous/src/main.py', {
            'post_nous_with_retries', '_retry_after_seconds',
            '_is_retriable_upstream_status', '_looks_model_capacity_error'},
            MODEL_REGISTRY=_registry(), same_provider_model_id=lambda *args: True,
            KEY_POOL=pool, _read_token_from_auth_path=lambda: None,
            post_nous=post, _parse_retry_after=parse_retry_after,
            _should_cooldown_key=should_cooldown_key,
            classify_upstream_error=classify_upstream_error)
        actual_status, result, key = await namespace['post_nous_with_retries'](payload)
        assert actual_status == 200 and result == success and key is None
    else:
        def request(method, url, json, headers, **kwargs):
            token = headers['Authorization'].removeprefix('Bearer ')
            calls.append((token, copy.deepcopy(json)))
            return _Upstream(status=status, reply=failure) if token == 'mock-exhausted-key' else _Upstream(reply=success)

        namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
        response = await namespace['_proxy_request']('POST', 'chat/completions', payload)
        assert response.status_code == 200 and json.loads(response.body) == success
        assert response.headers['x-upstream-marker'] == 'retained'
    assert calls == [('mock-exhausted-key', before), ('mock-working-key', before)]
    assert payload == before
    assert all(key.in_flight == 0 for key in pool.keys)


async def test_openrouter_rotation_excludes_uncoolable_attempted_credentials():
    pool = _pool('openrouter')
    entry_type = type(pool.keys[0])
    pool.keys.extend([entry_type('third', 'mock-third-key'), entry_type('fourth', 'mock-fourth-key')])
    for key in pool.keys:
        key.hard_rpm = 1000
        key.timestamps = [] if key.label == 'first' else [time.time()] * 100
    calls = []
    success = {'id': 'fourth-credential-success'}

    def request(method, url, json, headers, **kwargs):
        token = headers['Authorization'].removeprefix('Bearer ')
        calls.append(token)
        if token == 'mock-fourth-key':
            return _Upstream(reply=success)
        return _Upstream(status=403, reply={'error': {'message':
            '<html><title>Just a moment</title>Cloudflare challenge cf-ray</html>'}})

    namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
    response = await namespace['_proxy_request']('POST', 'chat/completions', {'model': 'native/model'})
    assert response.status_code == 200 and json.loads(response.body) == success
    assert len(calls) == len(set(calls)) == 4
    assert all(key.hard_blocked_until == 0 and not key.model_blocked_until and key.in_flight == 0
               for key in pool.keys)


@pytest.mark.parametrize('status,payload', [
    (400, {'error': {'message': 'invalid parameter'}}),
    (404, {'error': {'message': 'route not found'}}),
])
async def test_openrouter_deterministic_errors_do_not_spend_other_credentials(status, payload):
    pool = _pool('openrouter')
    calls = []

    def request(method, url, **kwargs):
        calls.append(url)
        return _Upstream(status=status, reply=payload)

    namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
    response = await namespace['_proxy_request']('POST', 'chat/completions', {'model': 'native/model'})
    assert response.status_code == status and json.loads(response.body) == payload
    assert response.headers['x-upstream-marker'] == 'retained'
    assert len(calls) == 1
    assert all(key.in_flight == 0 for key in pool.keys)


def test_payment_required_classification_rotates_account_credentials():
    classification = classify_upstream_error(402, {'error': {'message': 'credit balance depleted'}})
    assert classification.retry_same_model and classification.rotate_key and classification.account_scoped


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('stream', [False, True])
async def test_native_responses_forwards_new_fields_events_and_server_continuation(wrapper, stream):
    body = {'model': 'native/model', 'input': [{'type': 'future_input', 'payload': 'new'}],
            'stream': stream, 'previous_response_id': 'upstream-response-id',
            'tools': [{'type': 'future_native_tool', 'config': {'new': True}}],
            'future_parameter': {'nested': [1, None]}}
    reply = {'id': 'server-next-id', 'object': 'response', 'status': 'completed',
             'output': [{'type': 'future_output', 'encrypted': 'opaque'}],
             'previous_response_id': 'upstream-response-id', 'future_metadata': True}
    wire = (b'event: response.future_event\r\nid: server-event-id\r\n'
            b'data: {"future_metadata":true}\r\n\r\n'
            b'event: response.completed\ndata: {"response":{"id":"server-next-id"}}\n\n')
    upstream = _Upstream([wire[index:index + 1] for index in range(len(wire))])
    response = (upstream if wrapper == 'nous' else StreamingResponse(upstream)) if stream else (
        reply if wrapper == 'nous' else JSONResponse(reply))
    captured = []
    namespace = _route_namespace(wrapper, anthropic=False, response=response, captured=captured)
    result = await namespace['responses'](_request(body))
    assert result.status_code == 200
    assert await _collect(result) == wire if stream else json.loads(result.body) == reply
    args, kwargs = captured[0]
    assert args[0 if wrapper == 'nous' else 2] == body
    assert kwargs['path'] == 'v1/responses' if wrapper == 'nous' else args[1] == 'responses'
    if stream and wrapper == 'nous':
        assert upstream.release_count == 1


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
async def test_native_responses_endpoint_failure_never_downgrades_to_chat(wrapper):
    error = {'error': {'type': 'future_error', 'message': 'native endpoint unavailable',
                       'future_detail': {'x': 1}}, 'future_top_level': True}
    captured = []
    namespace = _route_namespace(wrapper, anthropic=False,
                                 response=error if wrapper == 'nous' else JSONResponse(error, status_code=404),
                                 captured=captured, status=404)
    result = await namespace['responses'](_request({'model': 'native/model', 'input': 'hi'}))
    assert result.status_code == 404 and json.loads(result.body) == error
    assert len(captured) == 1
    args, kwargs = captured[0]
    assert kwargs['path'] == 'v1/responses' if wrapper == 'nous' else args[1] == 'responses'


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status', [400, 429])
async def test_nous_upstream_error_metadata_and_retry_header_are_preserved(stream, status):
    error = {'error': {'type': 'future_error', 'message': 'upstream error',
                       'future_detail': {'x': 1}, 'retry_after': 7}, 'future_top_level': True}
    upstream = _Upstream(status=status, reply=error)
    upstream.headers.update({'retry-after': '42', 'content-type': 'application/json'})

    async def session():
        return SimpleNamespace(post=lambda *args, **kwargs: upstream)

    namespace = _load(ROOT / 'nous/src/main.py', {
        'post_nous', '_prepare_nous_request_metadata', '_UpstreamJSON', '_retry_after_seconds',
        '_native_json_response', '_native_response_headers'},
        NOUS_BASE='http://mock.invalid', get_session=session,
        CONNECT_TIMEOUT_SEC=1, STREAM_SOCK_READ_TIMEOUT_SEC=30,
        _normalize_upstream_error=normalize_upstream_error,
        _parse_retry_after=parse_retry_after, _sanitize_header_value=lambda value: value)
    actual_status, result = await namespace['post_nous'](
        {'model': 'native/model'}, 'mock-token', stream=stream)
    assert actual_status == status and dict(result) == error
    assert namespace['_retry_after_seconds'](result) == 42
    response = namespace['_native_json_response'](result, status)
    assert json.loads(response.body) == error and response.headers['retry-after'] == '42'
    assert upstream.release_count == 1


@pytest.mark.parametrize('mode,layer,expected', [
    (None, '1', True), ('auto', '1', True), ('native', '1', True), ('chat', '1', False),
    ('native', '2', False), ('chat', '2', False),
])
def test_responses_native_default_and_explicit_chat_policy(mode, layer, expected, monkeypatch):
    from common.compat import responses_use_native_upstream, validate_compat_layer
    monkeypatch.setenv('COMPATIBILITY_LAYER', layer)
    if mode is None:
        monkeypatch.delenv('RESPONSES_UPSTREAM_MODE', raising=False)
    else:
        monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', mode)
    assert validate_compat_layer() == layer
    assert responses_use_native_upstream() is expected


def test_invalid_responses_policy_fails_startup_validation(monkeypatch):
    from common.compat import validate_compat_layer
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'automatic_downgrade')
    with pytest.raises(ValueError, match='RESPONSES_UPSTREAM_MODE'):
        validate_compat_layer()


@pytest.mark.parametrize('status,payload,expected', [
    (405, {'error': {'message': 'method not allowed'}}, True),
    (501, {'error': {'message': 'not implemented'}}, True),
    (404, {'detail': 'Not Found'}, True),
    (404, {'error': {'message': 'Responses route not found'}}, True),
    (404, {'error': {'code': 'model_not_found', 'message': 'unknown model'}}, False),
    (404, {'error': {'message': 'model not found for account'}}, False),
    (400, {'status': 400, 'message': 'additional info: missing tags'}, False),
    (401, {'error': {'message': 'invalid credential'}}, False),
    (402, {'error': {'message': 'quota exhausted'}}, False),
    (429, {'error': {'message': 'rate limited'}}, False),
    (500, {'error': {'message': 'internal error'}}, False),
    (200, {'object': 'response', 'output': []}, False),
])
def test_auto_fallback_requires_confirmed_native_endpoint_unavailability(status, payload, expected, monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    assert responses_chat_fallback_allowed(status, payload) is expected
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'native')
    assert not responses_chat_fallback_allowed(status, payload)
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    monkeypatch.setenv('COMPATIBILITY_LAYER', '2')
    assert not responses_chat_fallback_allowed(status, payload)


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('status,payload', [
    (405, {'error': {'message': 'method not allowed'}}),
    (501, {'error': {'message': 'not implemented'}}),
    (404, {'detail': 'Not Found'}),
])
async def test_auto_route_tries_native_before_the_approved_chat_adapter(wrapper, status, payload, monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    calls = []
    conversions = []
    chat_reply = {'id': 'chat-result', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}
    namespace = _route_namespace(wrapper, anthropic=False, response=None, captured=[])

    async def nous_proxy(body, *, path='v1/chat/completions', **kwargs):
        calls.append((path, copy.deepcopy(body)))
        return (status, payload, None) if path.endswith('/responses') else (200, chat_reply, None)

    async def router_proxy(method, path, body, **kwargs):
        calls.append((path, copy.deepcopy(body)))
        return JSONResponse(payload, status_code=status) if path == 'responses' else JSONResponse(chat_reply)

    def to_chat(body, *args, **kwargs):
        conversions.append(copy.deepcopy(body))
        return {'model': body['model'], 'messages': [{'role': 'user', 'content': body['input']}], 'stream': False}

    async def store(*args):
        pass

    namespace.update({
        'post_nous_with_retries': nous_proxy, '_proxy_request': router_proxy,
        'responses_to_chat': to_chat, '_extract_principal': lambda request: 'tenant',
        '_request_principal': lambda request: 'tenant',
        'chat_to_responses': lambda *args: {'id': 'converted', 'object': 'response', 'output': []},
        '_remember_completed_response': lambda *args: None, 'store_conversation': store,
    })
    body = {'model': 'exact/model', 'input': 'hi', 'future_parameter': True}
    response = await namespace['responses'](_request(body))
    result = json.loads(response.body) if isinstance(response, JSONResponse) else response
    assert result['id'] == 'converted'
    assert calls[0] == ('v1/responses' if wrapper == 'nous' else 'responses', body)
    assert calls[1][0] == ('v1/chat/completions' if wrapper == 'nous' else 'chat/completions')
    assert calls[1][1]['model'] == body['model']
    assert conversions == [body]


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('stream', [False, True])
async def test_auto_unsupported_native_route_releases_without_cooling_chat_credentials(wrapper, stream, monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    pool = _pool(wrapper)
    error = {'error': {'message': 'not implemented'}}
    calls = []
    if wrapper == 'nous':
        async def post(body, token, **kwargs):
            calls.append(token)
            return 501, error

        namespace = _load(ROOT / 'nous/src/main.py', {
            'post_nous_with_retries', '_retry_after_seconds', '_is_retriable_upstream_status',
            '_looks_model_capacity_error'},
            MODEL_REGISTRY=_registry(), same_provider_model_id=lambda *args: True,
            KEY_POOL=pool, _read_token_from_auth_path=lambda: None,
            post_nous=post, _parse_retry_after=parse_retry_after,
            _should_cooldown_key=should_cooldown_key, classify_upstream_error=classify_upstream_error,
            _responses_chat_fallback_allowed=responses_chat_fallback_allowed)
        actual_status, result, key = await namespace['post_nous_with_retries'](
            {'model': 'native/model'}, stream=stream, path='v1/responses')
        assert actual_status == 501 and result == error and key is None
    else:
        upstream = _Upstream(status=501, reply=error)

        def request(*args, **kwargs):
            calls.append(kwargs['headers']['Authorization'])
            return upstream

        namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=request))
        response = await namespace['_proxy_request']('POST', 'responses', {'model': 'native/model'}, stream=stream)
        assert response.status_code == 501 and json.loads(response.body) == error
        assert upstream.release_count == 1
    assert len(calls) == 1
    assert all(key.in_flight == 0 and key.hard_blocked_until == 0 and not key.model_blocked_until
               for key in pool.keys)


@pytest.mark.parametrize('stream', [False, True])
async def test_nous_native_route_real_retry_and_transport_send_required_tags(stream, monkeypatch):
    monkeypatch.delenv('NOUS_REQUEST_TAGS', raising=False)
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'native')
    reply = {'id': 'native-reply', 'object': 'response', 'output': []}
    wire = b'event: response.completed\ndata: {"response":{"id":"native-reply"}}\n\n'
    upstream = _Upstream([wire], reply=reply)
    records = []

    def post(url, **kwargs):
        records.append((url, copy.deepcopy(kwargs['json'])))
        return upstream

    async def session():
        return SimpleNamespace(post=post)

    namespace = _route_namespace('nous', anthropic=False, response=None, captured=[])
    namespace.update({
        'NOUS_BASE': 'http://credential-free.invalid', 'get_session': session,
        'CONNECT_TIMEOUT_SEC': 1, 'STREAM_SOCK_READ_TIMEOUT_SEC': 30,
        '_normalize_upstream_error': normalize_upstream_error,
        '_parse_retry_after': parse_retry_after, '_should_cooldown_key': should_cooldown_key,
        'classify_upstream_error': classify_upstream_error, 'MODEL_REGISTRY': _registry(),
        'same_provider_model_id': lambda *args: True, '_read_token_from_auth_path': lambda: None,
        'KEY_POOL': _pool('nous'),
    })
    names = {'post_nous_with_retries', 'post_nous', '_UpstreamJSON', '_prepare_nous_request_metadata',
             '_retry_after_seconds', '_is_retriable_upstream_status', '_looks_model_capacity_error'}
    nodes = [node for node in ast.parse((ROOT / 'nous/src/main.py').read_text()).body
             if getattr(node, 'name', None) in names]
    assert len(nodes) == len(names)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), 'nous-native-transport', 'exec'), namespace)
    body = {'model': 'exact/model', 'input': 'hi', 'stream': stream, 'future_parameter': {'opaque': True}}
    response = await namespace['responses'](_request(body))
    assert await _collect(response) == wire if stream else json.loads(response.body) == reply
    assert records == [('http://credential-free.invalid/v1/responses', {
        **body, 'tags': ['product=wrapper-nous', 'client=wrapper-nous', 'user=wrapper-nous']})]
    assert all(key.in_flight == 0 for key in namespace['KEY_POOL'].keys)


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
def test_cross_anthropic_request_retains_system_image_and_tool_history(wrapper):
    namespace = _load(ROOT / wrapper / 'src/main.py',
                      {'anthropic_to_openai', '_validate_anthropic_chat_request', 'normalize_schema'} if wrapper == 'nous' else {'_anthropic_to_openai'},
                      resolve_model=lambda model: model,
                      strip_cache_control=strip_cache_control, _strip_cache=strip_cache_control)
    convert = namespace['anthropic_to_openai' if wrapper == 'nous' else '_anthropic_to_openai']
    body = {'model': 'native/model', 'max_tokens': 99, 'system': [{'type': 'text', 'text': 'system rule'}],
            'tools': [{'name': 'inspect', 'input_schema': {'type': 'object'}}],
            'tool_choice': {'type': 'tool', 'name': 'inspect'},
            'messages': [
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call-a',
                    'name': 'inspect', 'input': {'path': 'a'}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call-a', 'content': 'ok'}]},
                {'role': 'user', 'content': [{'type': 'image', 'source': {
                    'type': 'base64', 'media_type': 'image/png', 'data': 'aGVsbG8='}}]},
            ]}
    converted = convert(body)
    assert converted['model'] == 'native/model'
    assert converted['messages'][0] == {'role': 'system', 'content': 'system rule'}
    assert converted['messages'][1]['tool_calls'][0]['id'] == 'call-a'
    assert json.loads(converted['messages'][1]['tool_calls'][0]['function']['arguments']) == {'path': 'a'}
    assert converted['messages'][2] == {'role': 'tool', 'tool_call_id': 'call-a', 'content': 'ok'}
    assert converted['messages'][3]['content'][0]['image_url']['url'] == 'data:image/png;base64,aGVsbG8='
    assert converted['tool_choice'] == {'type': 'function', 'function': {'name': 'inspect'}}
