"""Unsupported native endpoints must not discard remote continuation context."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.responses import JSONResponse, StreamingResponse

from common.compat import responses_chat_fallback_allowed
from common.translations import repair_orphan_tool_messages

ROOT = Path(__file__).resolve().parents[1]


def route(wrapper, *, status=501, native_reply=None, principal='tenant-a', stream=False):
    path = ROOT / wrapper / 'src/main.py'
    names = {'responses', 'responses_to_chat', '_response_store_key'}
    if wrapper == 'nous':
        names |= {'get_stored_conversation', '_prune_response_store_locked'}
        # DASH fix: routes now call these metrics helpers — extract them too.
        names |= {'_result_usage_tokens', '_metrics_key_label', '_sniff_stream_usage'}
    else:
        names |= {'_get_stored_conversation', '_prune_response_store'}
    nodes = [node for node in ast.parse(path.read_text()).body if getattr(node, 'name', None) in names]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    records = []
    native_reply = native_reply or {'error': {'message': 'Native Responses route unavailable'}}
    chat_reply = {'id': 'adapter-response', 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]}

    async def proxy(*args, path='v1/chat/completions', **kwargs):
        body = args[0] if wrapper == 'nous' else args[2]
        actual_path = path if wrapper == 'nous' else args[1]
        records.append((actual_path, copy.deepcopy(body)))
        reply = native_reply if actual_path.endswith('responses') else chat_reply
        actual_status = status if actual_path.endswith('responses') else 200
        if wrapper == 'nous':
            return actual_status, reply, None
        return JSONResponse(reply, status_code=actual_status)

    async def authorized(*args):
        pass

    async def record(*args):
        pass

    async def store(*args):
        pass

    def discard(coro, *args):
        coro.close()

    ns = {
        'asyncio': asyncio, 'copy': copy, 'json': json, 'os': os, 'time': time,
        'JSONResponse': JSONResponse, 'StreamingResponse': StreamingResponse,
        'logger': logging.getLogger(__name__),
        '_auth_check': authorized, 'check_rate_limit': lambda *args: True,
        'validate_max_tokens_field': lambda *args, **kwargs: None,
        'free_only_enabled': lambda: False, '_check_free_only': lambda model: None,
        'resolve_model': lambda model: model, '_is_anthropic_upstream': lambda: False,
        '_responses_use_native_upstream': lambda: True,
        '_responses_chat_fallback_allowed': responses_chat_fallback_allowed,
        'post_nous_with_retries': proxy, '_proxy_request': proxy,
        '_build_forward_headers': lambda headers: dict(headers),
        '_extract_principal': lambda request: principal,
        '_request_principal': lambda request: principal,
        'metrics': SimpleNamespace(record=lambda **kwargs: None,
                                   record_stream_usage=lambda **kwargs: None,
                                   record_tokens=lambda *args, **kwargs: None),
        'record_model_result': record, '_fire_and_forget': discard,
        '_native_json_response': lambda body, status=200: JSONResponse(body, status_code=status),
        '_error_response': lambda body, status_code=500: JSONResponse(body, status_code=status_code),
        'repair_orphan_tool_messages': repair_orphan_tool_messages,
        'chat_to_responses': lambda *args: {'id': 'adapter-response', 'object': 'response', 'output': []},
        '_remember_completed_response': lambda *args: None, 'store_conversation': store,
        '_RESPONSE_STORE': OrderedDict(), '_RESPONSE_STORE_MAX': 200,
        '_RESPONSE_STORE_MAX_ENTRIES': 200, '_RESPONSE_STORE_TTL_SEC': 60,
        '_RESPONSE_STORE_MAX_BYTES': 1000000,
    }
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), ns)
    return ns, records


def request(body):
    async def body_json():
        return copy.deepcopy(body)
    return SimpleNamespace(json=body_json, headers={}, client=None)


def cache(ns, principal, response_id, messages, age=0):
    ns['_RESPONSE_STORE'][ns['_response_store_key'](principal, response_id)] = (
        time.time() - age, len(json.dumps(messages).encode()), copy.deepcopy(messages))


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status, error', [
    (405, {'error': {'message': 'method not allowed'}}),
    (501, {'error': {'message': 'not implemented'}}),
    (404, {'error': {'message': 'route not found'}}),
])
async def test_missing_remote_continuation_rejects_before_chat_request(wrapper, stream, status, error, monkeypatch):
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    namespace, records = route(wrapper, status=status, native_reply=error)
    body = {'model': 'exact/model', 'stream': stream, 'previous_response_id': 'native-remote-id',
            'input': [{'type': 'function_call_output', 'call_id': 'call-remote', 'output': 'done'}]}
    response = await namespace['responses'](request(body))
    result = json.loads(response.body)
    assert response.status_code == 400
    assert result['error']['code'] == 'previous_response_not_found'
    assert result['error']['type'] == 'invalid_request_error'
    assert 'full conversation' in result['error']['message']
    assert len(records) == 1 and records[0] == ('v1/responses' if wrapper == 'nous' else 'responses', body)


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
@pytest.mark.parametrize('unusable', ['different-tenant', 'expired'])
async def test_fallback_history_is_tenant_scoped_and_ttl_bound(wrapper, unusable, monkeypatch):
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    namespace, records = route(wrapper)
    cache(namespace, 'tenant-b' if unusable == 'different-tenant' else 'tenant-a', 'remote-id',
          [{'role': 'user', 'content': 'private history'}], age=61 if unusable == 'expired' else 0)
    response = await namespace['responses'](request({'model': 'exact/model', 'previous_response_id': 'remote-id', 'input': 'continue'}))
    assert response.status_code == 400
    assert json.loads(response.body)['error']['code'] == 'previous_response_not_found'
    assert len(records) == 1


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
async def test_valid_local_adapter_history_preserves_assistant_and_tool_context(wrapper, monkeypatch):
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    namespace, records = route(wrapper)
    history = [{'role': 'user', 'content': 'original'}, {'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call-local', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{"q":"x"}'}}]}]
    cache(namespace, 'tenant-a', 'local-adapter-id', history)
    body = {'model': 'exact/model', 'previous_response_id': 'local-adapter-id',
            'input': [{'type': 'function_call_output', 'call_id': 'call-local', 'output': 'done'}]}
    response = await namespace['responses'](request(body))
    result = json.loads(response.body) if isinstance(response, JSONResponse) else response
    assert result['id'] == 'adapter-response'
    assert len(records) == 2
    assert records[1][0] == ('v1/chat/completions' if wrapper == 'nous' else 'chat/completions')
    assert records[1][1]['messages'] == history + [{'role': 'tool', 'tool_call_id': 'call-local', 'content': 'done'}]
    assert records[1][1]['model'] == body['model']


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
async def test_existing_empty_local_history_does_not_count_as_unknown_id(wrapper, monkeypatch):
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    namespace, records = route(wrapper)
    cache(namespace, 'tenant-a', 'known-empty-id', [])
    response = await namespace['responses'](request({'model': 'exact/model', 'previous_response_id': 'known-empty-id', 'input': 'new turn'}))
    assert len(records) == 2
    result = json.loads(response.body) if isinstance(response, JSONResponse) else response
    assert result['id'] == 'adapter-response'


@pytest.mark.parametrize('wrapper', ['nous', 'openrouter'])
async def test_native_success_keeps_remote_previous_response_id_opaque(wrapper, monkeypatch):
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    reply = {'id': 'remote-reply', 'object': 'response', 'future_output': {'opaque': True}}
    namespace, records = route(wrapper, status=200, native_reply=reply)
    body = {'model': 'exact/model', 'previous_response_id': 'never-locally-stored-native-id', 'input': 'continue', 'future': True}
    response = await namespace['responses'](request(body))
    assert response.status_code == 200 and json.loads(response.body) == reply
    assert records == [('v1/responses' if wrapper == 'nous' else 'responses', body)]


def nvidia_route(monkeypatch, *, status=501, stored=None):
    """Lift the actual nested route; provide a private relative store module."""
    path = ROOT / 'nvidia-python/src/main.py'
    node = next(node for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.AsyncFunctionDef) and node.name == 'responses_api')
    node.decorator_list = []
    lookups, adapters = [], []
    module = ModuleType('nvidia_guard_fixture.responses_compat')
    module._extract_principal = lambda request: 'tenant-a'

    def lookup(principal, response_id):
        lookups.append((principal, response_id))
        return copy.deepcopy(stored)

    module._get_stored_conversation = lookup
    monkeypatch.setitem(sys.modules, module.__name__, module)

    async def native(request, path):
        return JSONResponse({'id': 'native-reply', 'error': {'message': 'route not found'}}, status_code=status)

    async def adapter(request, raw):
        adapters.append(json.loads(raw))
        return {'id': 'adapter-response', 'object': 'response', 'output': []}, None, 200

    namespace = {'__package__': 'nvidia_guard_fixture', 'json': json, 'logging': logging,
                 'JSONResponse': JSONResponse, 'StreamingResponse': StreamingResponse,
                 'logger': logging.getLogger(__name__), '_is_anthropic_upstream': lambda: False,
                 'self': SimpleNamespace(_handle_catch_all=native,
                                         responses_handler=SimpleNamespace(handle_responses_api=adapter))}
    definition = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(definition), str(path), 'exec'), namespace)
    return namespace['responses_api'], lookups, adapters


def nvidia_request(body):
    async def raw_body():
        return json.dumps(body).encode()
    return SimpleNamespace(body=raw_body, headers={})


@pytest.mark.parametrize('status', [405, 501, 404])
@pytest.mark.parametrize('stream', [False, True])
async def test_nvidia_unknown_native_context_rejects_before_legacy_adapter(status, stream, monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    handler, lookups, adapters = nvidia_route(monkeypatch, status=status)
    response = await handler(nvidia_request({'model': 'exact/model', 'stream': stream,
                                            'previous_response_id': 'remote-id', 'input': 'continue'}))
    assert response.status_code == 400
    assert json.loads(response.body)['error']['code'] == 'previous_response_not_found'
    assert lookups == [('tenant-a', 'remote-id')]
    assert adapters == []


@pytest.mark.parametrize('history', [[], [{'role': 'user', 'content': 'stored turn'}]])
async def test_nvidia_known_adapter_context_is_allowed(history, monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    handler, lookups, adapters = nvidia_route(monkeypatch, stored=history)
    body = {'model': 'exact/model', 'previous_response_id': 'local-id', 'input': 'continue'}
    response = await handler(nvidia_request(body))
    assert response.status_code == 200 and json.loads(response.body)['id'] == 'adapter-response'
    assert lookups == [('tenant-a', 'local-id')]
    assert adapters == [body]


async def test_nvidia_native_success_does_not_consult_adapter_store(monkeypatch):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    handler, lookups, adapters = nvidia_route(monkeypatch, status=200)
    response = await handler(nvidia_request({'model': 'exact/model', 'previous_response_id': 'remote-id', 'input': 'continue'}))
    assert response.status_code == 200
    assert lookups == adapters == []
