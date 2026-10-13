"""Native NVIDIA provider diagnostics survive JSON, SSE and context recovery."""
import ast
import json
from types import SimpleNamespace

import pytest

from common.http_headers import upstream_response_headers
from test_active_native_passthrough import _collect
from test_nvidia_messages_request_controls import SOURCE, Peer, _request, _server


def _native_server(peers):
    server, namespace = _server(peers, [], native=True)
    tree = ast.parse(SOURCE.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
    cls.body = [node for node in cls.body if getattr(node, 'name', None) in
                ('_compat2_messages', '_stream_proxy')]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), cls], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE), 'exec'), namespace)
    for name in ('_compat2_messages', '_stream_proxy'):
        setattr(server, name, getattr(namespace['Server'], name).__get__(server))
    return server


def _headers(peer):
    peer.headers.update({
        'Request-ID': 'native-request-id', 'x-ratelimit-remaining-requests': '7',
        'Retry-After': '13', 'future-provider-metadata': 'opaque',
        'content-length': '999', 'content-encoding': 'gzip',
        'connection': 'keep-alive, x-private-hop', 'x-private-hop': 'hop-only',
        'transfer-encoding': 'chunked', 'trailer': 'x-footer',
        'cache-control': 'no-store',
        'Authorization': 'Bearer synthetic-provider-secret',
        'X-Api-Key': 'synthetic-provider-secret', 'Set-Cookie': 'session=private',
    })
    return peer


@pytest.mark.parametrize('surface', ['chat', 'messages', 'responses', 'proxy_post'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('status', [200, 201, 202, 400, 401, 403, 422, 500])
async def test_native_headers_preserved_and_transport_framing_removed(surface, stream, status):
    wire = b'event: future_provider_event\ndata: {"opaque":true}\n\n'
    reply = ({'id': 'native-result', 'future_data': [None, True]} if status < 400 else
             {'error': {'message': 'invalid request', 'future_error': {'opaque': True}}})
    peer = _headers(Peer([wire], status=status, reply=None) if stream and status < 400
                    else Peer(status=status, reply=reply))
    peer.headers['content-type'] = ('text/event-stream; charset=utf-8' if stream and status < 400
                                    else 'application/vnd.provider+json; charset=utf-8')
    server = _native_server([peer])
    body = {'model': 'native/exact-model', 'max_tokens': 100, 'stream': stream,
            'messages': [{'role': 'user', 'content': 'keep this'}]}
    if surface == 'chat':
        result = await server.proxy_openai(body, {}, body['model'], _request(body))
        headers, actual_status = result['headers'], result['status']
        actual = b''.join([part async for part in result['stream']]) if stream and status < 400 else result['data']
    else:
        if surface == 'messages':
            result = await server._compat2_messages(body, _request(body))
        elif surface == 'responses':
            result = await server._handle_catch_all(_request(body), '/v1/responses')
        else:
            result = await server._proxy_post(_request(body), body, json.dumps(body).encode(),
                body['model'], '/v1/completions', lambda key: 'http://synthetic.invalid/v1/completions')
        headers, actual_status = result.headers, result.status_code
        actual = await _collect(result) if stream and status < 400 else json.loads(result.body)
        if not (stream and status < 400):
            assert headers['content-length'] == str(len(result.body))
    assert actual_status == status
    assert actual == (wire if stream and status < 400 else reply)
    assert headers['request-id'] == 'native-request-id'
    assert headers['x-ratelimit-remaining-requests'] == '7'
    assert headers['retry-after'] == '13'
    assert headers['future-provider-metadata'] == 'opaque'
    assert headers['content-type'] == peer.headers['content-type']
    assert headers['cache-control'] == 'no-store'
    for name in ('content-encoding', 'x-private-hop', 'transfer-encoding', 'trailer',
                 'connection', 'authorization', 'x-api-key', 'set-cookie'):
        assert name not in headers
    assert headers.get('content-length') != '999'
    assert peer.release_count == 1 and server._in_flight == 0
    assert all(key.in_flight == 0 for key in server.pool.keys)


@pytest.mark.parametrize('stream', [False, True])
async def test_recovery_headers_merge_with_success_provider_headers(stream):
    error = Peer(status=400, reply={'error': {'message': 'max_tokens must be <= 50'}})
    wire = b'data: {"opaque":true}\n\n'
    success = _headers(Peer([wire]) if stream else Peer(reply={'id': 'native-result'}))
    server = _native_server([error, success])
    body = {'model': 'native/exact-model', 'max_tokens': 100, 'stream': stream,
            'messages': [{'role': 'user', 'content': 'keep this'}]}
    response = await server._compat2_messages(body, _request(body))
    if stream:
        assert await _collect(response) == wire
    assert response.headers['request-id'] == 'native-request-id'
    assert response.headers['x-context-recovery'] == 'reduced_output'
    assert response.headers['x-context-output-tokens'] == '50'
    assert all(peer.release_count == 1 for peer in (error, success))


def test_response_header_filter_sanitizes_and_context_annotation_wins():
    response = SimpleNamespace(headers={
        'X-Metadata': 'opaque\r\nvalue', 'Connection': 'X-Hop', 'X-Hop': 'private',
        'X-Context-Recovery': 'stale'},
        _context_recovery_headers={'X-Context-Recovery': 'reduced_output'})
    assert upstream_response_headers(response) == {
        'x-metadata': 'opaquevalue', 'x-context-recovery': 'reduced_output'}
