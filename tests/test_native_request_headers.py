"""Native request extensions survive without forwarding local credentials.

Production functions run against recording peers with fake keys, without
wrapper startup, dotenv, production state, or requests to an external service.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from multidict import CIMultiDict
from starlette.datastructures import Headers

from common.translations.shared import build_forward_headers
from common.wrapper_scope import select_wrappers

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = select_wrappers(include_registry=False)
EXTENSIONS = {
    'Anthropic-Version': '2023-06-01',
    'Anthropic-Beta': 'future-beta-2099-01-01',
    'OpenAI-Beta': 'responses=experimental',
    'Idempotency-Key': 'opaque-stable-retry-id',
    'X-Stainless-Timeout': '613',
    'X-Future-SDK-Feature': 'opaque; version=7',
    'Future-Protocol-Field': '  unmodified value  ',
    'X-Request-ID': 'caller-request-id',
    'User-Agent': 'future-agent/2099.1',
}
LOCAL = {
    'Authorization': 'Bearer local-bearer-must-not-leak',
    'X-API-Key': 'local-anthropic-key-must-not-leak',
    'API-Key': 'local-api-key-must-not-leak',
    'Cookie': 'local-session=must-not-leak',
    'Proxy-Authorization': 'local-proxy-must-not-leak',
    'Host': 'local-wrapper.invalid',
    'Content-Length': '999999',
    'Content-Type': 'local/incorrect',
    'Content-Encoding': 'gzip',
    'Accept-Encoding': 'br',
    'Connection': 'keep-alive, X-Private-Hop',
    'X-Private-Hop': 'private-hop-must-not-leak',
    'Keep-Alive': 'timeout=99',
    'Transfer-Encoding': 'chunked',
    'Trailer': 'X-Private-Trailer',
    'Forwarded': 'for=private-local-client',
    'X-Forwarded-For': 'private-local-client',
    'X-Forwarded-Host': 'private-local-host',
    'X-Forwarded-Future': 'private-proxy-metadata',
    'X-Real-IP': 'private-local-client',
    'Via': 'private-local-proxy',
}


@pytest.mark.parametrize('factory', [dict, Headers, CIMultiDict])
def test_future_headers_survive_case_insensitive_filter_and_local_credentials_do_not(factory):
    actual = build_forward_headers(factory({**EXTENSIONS, **LOCAL}))
    assert actual == {key.lower(): value for key, value in EXTENSIONS.items()}


def test_all_connection_header_instances_nominate_fields_case_insensitively():
    headers = CIMultiDict([
        ('Connection', 'X-First, Keep-Alive'), ('connection', '  x-second  '),
        ('X-First', 'hop-one'), ('X-Second', 'hop-two'), ('X-Future', 'native'),
    ])
    assert build_forward_headers(headers) == {'x-future': 'native'}


def test_trusted_overrides_are_case_insensitive_and_cannot_restore_hop_fields():
    headers = {'User-Agent': 'caller', 'Authorization': 'local-secret',
               'Connection': 'x-hop', 'X-Hop': 'local'}
    overrides = {'USER-AGENT': 'operator', 'Authorization': 'Bearer provider-key',
                 'Content-Type': 'application/json', 'X-Hop': 'blocked',
                 'Connection': 'X-Other-Hop', 'X-Other-Hop': 'blocked'}
    assert build_forward_headers(headers, overrides) == {
        'user-agent': 'operator', 'authorization': 'Bearer provider-key',
        'content-type': 'application/json',
    }


def test_valid_opaque_values_are_not_truncated_and_invalid_fields_cannot_inject_headers():
    long_value = 'opaque' * 2000
    actual = build_forward_headers({
        'X-Large-Extension': long_value, 'X-Invalid\r\nHeader': 'bad',
        'X-Invalid Name': 'bad', 'X-Safe': 'first\r\nsecond\x00\x7f',
        'X-None': None, 'X-Empty': '',
    })
    assert actual == {'x-large-extension': long_value, 'x-safe': 'firstsecond', 'x-empty': ''}


def _nvidia_forwarder():
    path = ROOT / 'nvidia-python/src/main.py'
    tree = ast.parse(path.read_text())
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'forward_headers')
    namespace = {'Request': object, '_build_forward_headers': build_forward_headers,
                 'generate_request_id': lambda: 'generated-request-id'}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['forward_headers']


def test_nvidia_preserves_caller_request_id_and_generates_one_only_when_missing():
    forward = _nvidia_forwarder()
    assert forward(SimpleNamespace(headers={'X-Request-ID': 'caller'}))['x-request-id'] == 'caller'
    assert forward(SimpleNamespace(headers={}))['x-request-id'] == 'generated-request-id'


@pytest.mark.parametrize('wrapper', ACTIVE)
@pytest.mark.parametrize('surface', ['chat/completions', 'messages', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
async def test_actual_native_transports_forward_extensions_and_replace_local_auth(wrapper, surface, stream):
    from test_active_native_passthrough import _collect, _openrouter_proxy_namespace, _pool
    from test_nvidia_messages_request_controls import Peer, _request, _server

    body = {'model': 'native/exact-model', 'stream': stream, 'max_tokens': 100,
            'messages': [{'role': 'user', 'content': 'unchanged'}]}
    request = _request(body)
    request.headers = Headers({**EXTENSIONS, **LOCAL})
    wire = b'event: future\ndata: {"unchanged":true}\n\n'
    peer = Peer([wire]) if stream else Peer(reply={'id': 'unchanged'})

    if wrapper == 'nvidia-python':
        records = []
        server, namespace = _server([peer], records, native=True)
        namespace['forward_headers'] = forward = _nvidia_forwarder()
        if surface == 'chat/completions':
            result = await server.proxy_openai(body, forward(request), body['model'], request)
            if stream:
                assert b''.join([part async for part in result['stream']]) == wire
        elif surface == 'messages':
            status, result, key = await server._compat2_proxy(body, request)
            assert status == 200
            if stream:
                result.release()
                key.decrement_in_flight()
                server._in_flight -= 1
        else:
            result = await server._handle_catch_all(request, '/v1/responses')
            if stream:
                assert await _collect(result) == wire
        sent = records[0][1]['headers']
        provider_key = 'synthetic-credential-0'
        assert all(key.in_flight == 0 for key in server.pool.keys)
    elif wrapper == 'nous':
        from test_nous_required_metadata import transport
        post, records = transport()
        status, result = await post(body, 'synthetic-provider-key', stream=stream,
                                   extra_headers=build_forward_headers(request.headers),
                                   path='v1/' + surface)
        assert status == 200
        if stream:
            result.release()
        sent = records[0]['headers']
        provider_key = 'synthetic-provider-key'
    elif wrapper == 'openrouter':
        records = []

        def send(*args, **kwargs):
            records.append(kwargs)
            return peer

        pool = _pool(wrapper)
        namespace = _openrouter_proxy_namespace(pool, SimpleNamespace(request=send))
        namespace['_build_forward_headers'] = build_forward_headers
        result = await namespace['_proxy_request']('POST', surface, body, request=request, stream=stream)
        assert result.status_code == 200
        if stream:
            assert await _collect(result) == wire
        sent = records[0]['headers']
        provider_key = 'mock-exhausted-key'
        assert all(key.in_flight == 0 for key in pool.keys)
    else:
        pytest.fail(f'Native transport fixture is required for active provider {wrapper}')

    normalized = {key.lower(): value for key, value in sent.items()}
    for key, value in EXTENSIONS.items():
        assert normalized[key.lower()] == value
    assert normalized['authorization'] == f'Bearer {provider_key}'
    assert normalized['content-type'] == 'application/json'
    # Wrappers may set their own accept-encoding; every other local field is
    # absent unless it is the intentionally replaced auth/content type.
    for key in LOCAL:
        if key.lower() not in {'authorization', 'content-type', 'accept-encoding'}:
            assert key.lower() not in normalized
    assert all('must-not-leak' not in str(value) for value in normalized.values())
