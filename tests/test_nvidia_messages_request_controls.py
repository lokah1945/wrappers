"""Exercise NVIDIA's cross Messages route, real converter, pool and transport.

AST loading and private package imports avoid dotenv, startup and production
state. The upstream peer and credentials are entirely synthetic.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import importlib.util
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, Response
from common.sse import ClosingStreamingResponse as StreamingResponse

from common.compat import passthrough_anthropic_sse
from common.model import classify_upstream_error
from common.translations.shared import openai_to_anthropic_response
from test_active_native_passthrough import _Upstream, _collect

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'nvidia-python/src/main.py'
MODEL = 'nvidia/nemotron-3-super-120b-a12b'


def _module(name, path, *, package=None):
    if package and package not in sys.modules:
        parent = ModuleType(package)
        parent.__path__ = [str(path.parent)]
        sys.modules[package] = parent
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


COMPAT = _module('nvidia_controls_fixture.anthropic_compat',
                 ROOT / 'nvidia-python/src/anthropic_compat.py', package='nvidia_controls_fixture')
POOL = _module('nvidia_controls_fixture.key_pool', ROOT / 'nvidia-python/src/key_pool.py')


class Peer(_Upstream):
    async def read(self):
        return json.dumps(self.reply).encode()


def _server(peers, records, *, native=False, keys=1):
    tree = ast.parse(SOURCE.read_text())
    functions = {'_translate_nemotron_super_controls', 'translate_thinking_to_nim',
                 'find_reasoning_config'}
    nodes = [node for node in tree.body if getattr(node, 'name', None) in functions]
    nodes += [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == 'REASONING_CONFIGS' for target in node.targets)]
    server_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
    methods = {'_handle_anthropic_messages', 'proxy_openai', '_prepare_proxy_body',
               '_compat2_proxy', '_proxy_post', '_handle_catch_all', '_stream_proxy'}
    server_class.body = [node for node in server_class.body if getattr(node, 'name', None) in methods]
    nodes.append(server_class)

    async def safe_body(peer):
        return json.loads(await peer.text())

    namespace = {
        'json': json, 'asyncio': asyncio, 'time': time, 'os': os,
        'logger': logging.getLogger(__name__), 'Request': object,
        'JSONResponse': JSONResponse, 'Response': Response, 'StreamingResponse': StreamingResponse,
        '_is_anthropic_upstream': lambda: native, '_is_reasoning_injection_disabled': lambda model: False,
        'validate_anthropic_request': COMPAT.validate_anthropic_request,
        'anthropic_to_openai': COMPAT.anthropic_to_openai,
        'anthropic_error': COMPAT.anthropic_error,
        'openai_to_anthropic': COMPAT.openai_to_anthropic,
        'openai_to_anthropic_response': openai_to_anthropic_response,
        'stream_openai_to_anthropic': COMPAT.stream_openai_to_anthropic,
        'estimate_input_tokens': COMPAT.estimate_input_tokens,
        'resolve_target_model': lambda model: model, 'is_model_unavailable': lambda model: False,
        'guard_stream_unsupported': lambda *args: None, 'clamp_max_tokens_for_model': lambda *args: None,
        'apply_default_reasoning': lambda *args: None, 'forward_headers': lambda request: {},
        'sanitize_nvidia_payload': lambda body: None, 'MAX_RETRIES': 0, 'DEFAULT_PARAMS': {},
        'BASE_LLM': 'http://synthetic.invalid', 'route_upstream': lambda path: 'http://synthetic.invalid',
        '_safe_response_body': safe_body, '_normalize_upstream_error': lambda status, body, model: (status, body),
        'classify_upstream_error': classify_upstream_error,
        'same_provider_model_id': lambda provider, first, second: first == second,
        'MODEL_REGISTRY': SimpleNamespace(call_plan=lambda model, surface: SimpleNamespace(
            model=SimpleNamespace(provider_model_id=model), path='/v1/chat/completions')),
        '_passthrough_anthropic_sse': passthrough_anthropic_sse,
        '_parse_retry_after': lambda value: 30,
        'model_from_path': lambda path: '', '_env_flag': lambda *args: False,
    }
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    server = namespace['Server']()
    server.pool = POOL.KeyPool()
    server.pool.keys = [POOL.KeyEntry(f'key-{index}', f'synthetic-credential-{index}') for index in range(keys)]
    server.registry = server.metrics = None
    server._in_flight = 0
    server._client_timeout = lambda *args: None
    server._normalize_path = lambda path: path
    server._classify_retry = lambda status, classification: classification['retry_same_model']

    # R-DEAD1/R-HDR1/R-OBS1 seams: fast-fail, per-attempt logging and the
    # headers deadline are stubbed — they are exercised by their own tests.
    async def _fastfail_none(model_id):
        return None
    server._fastfail_dead_model_reason = _fastfail_none
    server._log_attempt = lambda *args, **kwargs: None
    server._headers_deadline_sec = lambda *args, **kwargs: None

    async def record(*args):
        pass

    server._record_model_response = record

    def post(url, **kwargs):
        records.append((url, copy.deepcopy(kwargs)))
        return peers[len(records) - 1]

    def request(method, url, **kwargs):
        return post(url, **kwargs)

    server._session = SimpleNamespace(post=post, request=request)
    return server, namespace


def _request(body):
    async def raw():
        return json.dumps(body).encode()
    return SimpleNamespace(headers={}, body=raw, method='POST', url=SimpleNamespace(query=''))


def _body(controls=None):
    return {'model': MODEL, 'max_tokens': 4096,
            'messages': [{'role': 'user', 'content': 'Synthetic input.'}], **copy.deepcopy(controls or {})}


CONTROLS = [
    ({'thinking': {'type': 'disabled'}}, 'none', None),
    ({'thinking': {'type': 'enabled'}}, 'high', None),
    ({'thinking': {'type': 'adaptive'}}, 'high', None),
    ({'thinking': {'type': 'enabled', 'budget_tokens': 2048}}, 'high', 2048),
    ({'output_config': {'effort': 'low'}}, 'low', None),
    ({'output_config': {'effort': 'high'}}, 'high', None),
    ({'thinking': {'type': 'adaptive'}, 'output_config': {'effort': 'low'}}, 'low', None),
    ({'thinking': {'type': 'disabled'}, 'output_config': {'effort': 'high'}}, 'none', None),
]


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('controls,effort,budget', CONTROLS)
async def test_messages_controls_reach_actual_transport(stream, controls, effort, budget):
    wire = (b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n')
    peer = Peer([wire]) if stream else Peer(reply={'id': 'synthetic', 'choices': [
        {'message': {'content': 'OK'}, 'finish_reason': 'stop'}]})
    records = []
    server, _ = _server([peer], records)
    body = _body(controls)
    body['stream'] = stream
    original = copy.deepcopy(body)
    response = await server._handle_anthropic_messages(json.dumps(body).encode(), _request(body))
    assert response.status_code == 200
    if stream:
        assert b'event: message_stop\n' in await _collect(response)
    else:
        assert json.loads(response.body)['content'] == [{'type': 'text', 'text': 'OK'}]
    assert len(records) == 1 and records[0][0].endswith('/v1/chat/completions')
    sent = records[0][1]['json']
    assert sent['reasoning_effort'] == effort
    assert sent['chat_template_kwargs'] == {
        'enable_thinking': effort != 'none', 'low_effort': effort == 'low', 'force_nonempty_content': True}
    assert sent.get('reasoning_budget') == budget
    assert sent['model'] == MODEL and sent['max_tokens'] == 4096
    assert body == original and peer.release_count == 1
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_mapped_controls_win_conflicts_and_preserve_explicit_extensions():
    body = _body({'thinking': {'type': 'enabled', 'budget_tokens': 2048},
                  'output_config': {'effort': 'low'}, 'reasoning_effort': 'high', 'reasoning_budget': 99,
                  'chat_template_kwargs': {'force_nonempty_content': False, 'reasoning_budget': 7,
                                           'future_extension': {'opaque': [None, True]}}})
    records = []
    peer = Peer(reply={'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]})
    server, _ = _server([peer], records)
    response = await server._handle_anthropic_messages(json.dumps(body).encode(), _request(body))
    assert response.status_code == 200
    sent = records[0][1]['json']
    assert sent['reasoning_effort'] == 'low' and sent['reasoning_budget'] == 2048
    assert sent['chat_template_kwargs'] == {'force_nonempty_content': False, 'reasoning_budget': 2048,
        'future_extension': {'opaque': [None, True]}, 'enable_thinking': True, 'low_effort': True}


async def test_absent_anthropic_controls_preserve_explicit_provider_options():
    controls = {'reasoning_effort': 'low', 'reasoning_budget': 777,
                'chat_template_kwargs': {'force_nonempty_content': False, 'future_extension': True}}
    records = []
    server, _ = _server([Peer(reply={'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}]})], records)
    body = _body(controls)
    assert (await server._handle_anthropic_messages(json.dumps(body).encode(), _request(body))).status_code == 200
    sent = records[0][1]['json']
    assert all(sent[key] == value for key, value in controls.items())


INVALID = [
    {'tools': [None]}, {'tools': None}, {'tools': {}},
    {'tools': [{'name': 'lookup', 'input_schema': []}]},
    {'tools': [{'name': None, 'input_schema': {}}]},
    {'messages': None}, {'messages': [None]},
    {'messages': [{'role': 'user', 'content': [None]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': None}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': 'invalid'}]}]},
    {'messages': [{'role': 'user', 'content': [{'type': 'image', 'source': {'url': None}}]}]},
    {'messages': [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': None}]}]},
    {'system': [{'type': 'text', 'text': None}]}, {'thinking': True}, {'output_config': []},
    {'thinking': {}}, {'thinking': {'type': 'future_unrepresented_mode'}},
    *[{'output_config': {'effort': effort}} for effort in ('medium', 'xhigh', 'max', ['low'])],
    *[{'thinking': {'type': 'enabled', 'budget_tokens': budget}}
      for budget in (0, -1, 32769, True, '2048', 4096)],
]


@pytest.mark.parametrize('changes', INVALID)
async def test_invalid_cross_request_returns_anthropic_400_before_upstream(changes):
    records = []
    server, _ = _server([], records)
    body = _body(changes)
    response = await server._handle_anthropic_messages(json.dumps(body).encode(), _request(body))
    assert response.status_code == 400
    error = json.loads(response.body)
    assert error['type'] == 'error' and error['error']['type'] == 'invalid_request_error'
    assert error['error']['message'] and records == []
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


@pytest.mark.parametrize('stream', [False, True])
async def test_matching_native_messages_bypass_conversion_and_cross_validation(stream):
    records = []
    server, namespace = _server([], records, native=True)
    body = {'model': MODEL, 'stream': stream, 'tools': [None], 'messages': [
        {'role': 'future_role', 'content': [{'type': 'future_native_content', 'opaque': None}]}],
        'thinking': {'type': 'future_mode'}, 'output_config': {'effort': 'max'}, 'future_parameter': True}
    original = copy.deepcopy(body)

    def forbidden(*args):
        pytest.fail('matching native Messages invoked cross conversion/validation')

    namespace['validate_anthropic_request'] = namespace['anthropic_to_openai'] = forbidden
    namespace['translate_thinking_to_nim'] = forbidden
    wire = b'event: future_v99\ndata: {"future":true}\n\n'

    async def native(payload, request):
        assert payload == original
        return StreamingResponse(iterate()) if stream else JSONResponse({'future_reply': True})

    async def iterate():
        yield wire

    server._compat2_messages = native
    response = await server._handle_anthropic_messages(json.dumps(body).encode(), _request(body))
    assert await _collect(response) == wire if stream else json.loads(response.body) == {'future_reply': True}
    assert body == original and records == []


@pytest.mark.parametrize('method', ['proxy_openai', '_compat2_proxy', '_proxy_post', '_handle_catch_all'])
@pytest.mark.parametrize('failure_status', [401, 429])
async def test_seventh_available_key_succeeds_even_with_zero_max_retries(method, failure_status):
    failures = [Peer(status=failure_status, reply={'error': {'message':
        'API key rate limit exceeded' if failure_status == 429 else 'Invalid API key'}}) for _ in range(6)]
    success = Peer(reply={'marker': 'seventh-key-success'})
    records = []
    server, _ = _server([*failures, success], records, keys=7)
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Synthetic input.'}]}
    request = _request(body)
    if method == 'proxy_openai':
        response = await server.proxy_openai(body, {}, MODEL, request)
        assert response['status'] == 200 and response['data']['marker'] == 'seventh-key-success'
    elif method == '_compat2_proxy':
        status, response, key = await server._compat2_proxy(body, request)
        assert status == 200 and response['marker'] == 'seventh-key-success' and key is None
    elif method == '_proxy_post':
        response = await server._proxy_post(request, body, b'', MODEL, '/v1/embeddings',
                                            lambda key: 'http://synthetic.invalid/v1/embeddings')
        assert response.status_code == 200
    else:
        response = await server._handle_catch_all(request, '/v1/responses')
        assert response.status_code == 200
    assert len(records) == 7
    assert len({entry[1]['headers']['Authorization'] for entry in records}) == 7
    assert all(entry[1]['json']['model'] == MODEL for entry in records)
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)
    assert all(peer.release_count == 1 for peer in [*failures, success])
