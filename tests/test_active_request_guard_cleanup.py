"""Reject null bodies and keep cached discovery independent of inference keys.

No wrapper startup, environment file, provider socket or production state is
used. The discovery route runs with its real key pool and controlled transport.
"""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from common.body_guard import JSONBodyGuard

ROOT = Path(__file__).resolve().parents[1]


async def _guard_request(raw, path, content_type=b'application/json'):
    entered = []
    replies = []
    messages = iter([
        {'type': 'http.request', 'body': raw[:2], 'more_body': True},
        {'type': 'http.request', 'body': raw[2:], 'more_body': False},
    ])

    async def receive():
        return next(messages, {'type': 'http.disconnect'})

    async def send(message):
        replies.append(message)

    async def downstream(scope, receive, send):
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        entered.append(b''.join(chunks))

    scope = {'type': 'http', 'method': 'POST', 'path': path,
             'headers': [(b'content-type', content_type)]}
    await JSONBodyGuard(downstream)(scope, receive, send)
    return entered, replies


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses', '/v1/messages'])
@pytest.mark.parametrize('raw', [b'null', b'[]', b'42', b'false', b'"text"'])
@pytest.mark.parametrize('content_type', [b'application/json', b'application/x-www-form-urlencoded', b'text/plain'])
async def test_non_object_json_rejected_before_route(raw, path, content_type):
    entered, replies = await _guard_request(raw, path, content_type)
    assert not entered
    assert replies[0]['status'] == 400
    error = json.loads(replies[1]['body'])
    assert error['error']['type'] == 'invalid_request_error'
    if path == '/v1/messages':
        assert error['type'] == 'error'
    else:
        assert error['error']['code'] == 'invalid_body_shape'
    if raw == b'null':
        assert 'got null' in error['error']['message']


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [b'{}', b'{"model":"exact/model"}', b'{bad-json', b''])
async def test_object_or_malformed_json_keeps_route_validation(raw):
    entered, replies = await _guard_request(raw, '/v1/responses')
    assert entered == [raw]
    assert replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/v1/chat/completions', '/v1/responses', '/v1/messages'])
@pytest.mark.parametrize('content_type', [b'application/x-www-form-urlencoded', b'text/plain'])
async def test_mislabeled_native_object_preserves_exact_bytes_and_future_fields(path, content_type):
    raw = b' {"model":"exact/model", "future_parameter":{"opaque":[null,true]}, "tools":[{"type":"future_tool"}]} '
    entered, replies = await _guard_request(raw, path, content_type)
    assert entered == [raw]
    assert replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize('path,content_type,raw', [
    ('/v1/files', b'application/octet-stream', b'\x00\xff\x80binary-upload'),
    ('/v1/files', b'text/plain', b'null'),
    ('/v1/audio/transcriptions', b'multipart/form-data; boundary=boundary',
     b'--boundary\r\nContent-Disposition: form-data; name="file"\r\n\r\nnull\r\n--boundary--'),
])
async def test_non_json_upload_paths_keep_original_body(path, content_type, raw):
    entered, replies = await _guard_request(raw, path, content_type)
    assert entered == [raw]
    assert replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize('content_type', [b'application/json', b'application/x-www-form-urlencoded'])
async def test_explicit_non_object_opt_out_precedes_inference_shape_guard(monkeypatch, content_type):
    import common.body_guard as guard
    monkeypatch.setattr(guard, '_ALLOW_NON_OBJECT_PATHS', frozenset({'/v1/responses'}))
    entered, replies = await _guard_request(b'null', '/v1/responses', content_type)
    assert entered == [b'null']
    assert replies == []


def _load_discovery(pool, get_agent, tmp_path):
    from common.model import LocalModelRegistry
    from common.model.metadata import discovery_limits
    from common.model_catalog import CachedModelCatalog
    from common.model_state import ModelStateStore
    path = ROOT / 'openrouter/src/main.py'
    tree = ast.parse(path.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name in ('list_models', '_fetch_model_catalog')]
    for function in functions:
        function.decorator_list = []
    store = ModelStateStore('openrouter', tmp_path / 'models.db')
    registry = LocalModelRegistry('openrouter', ROOT / 'model-registry')
    cache = CachedModelCatalog(store, registry, SimpleNamespace(schedule_catalog=lambda *a: None),
                               'http://controlled.invalid/v1/models')
    namespace = {'pool': pool, 'get_agent': get_agent, 'JSONResponse': JSONResponse,
                 'OPENROUTER_BASE': 'http://controlled.invalid/v1', 'MODEL_CATALOG': cache,
                 'free_only_enabled': lambda: False, 'logger': logging.getLogger(__name__),
                 'discovery_limits': discovery_limits}
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[
        ast.alias(name='annotations')], level=0), *functions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['list_models'], cache


def _real_pool(monkeypatch):
    monkeypatch.setenv('LOAD_SHEDDING_ENABLED', 'true')
    monkeypatch.setenv('INFLIGHT_SOFT_CAP', '1')
    monkeypatch.setenv('OPENROUTER_HARD_LIMIT_RPM', '1000')
    monkeypatch.setenv('OPENROUTER_SOFT_LIMIT_RPM', '1000')
    path = ROOT / 'openrouter/src/key_pool.py'
    spec = importlib.util.spec_from_file_location('controlled_openrouter_pool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pool = module.KeyPool()
    pool.keys = [module.KeyEntry('controlled', 'synthetic-credential')]
    original_release = pool.release
    releases = []

    def release(key):
        releases.append(key)
        original_release(key)

    pool.release = release
    return pool, releases


class _Transport:
    def __init__(self, outcome):
        self.outcome = outcome
        self.waiting = asyncio.Event()
        self.calls = 0
        self.closed = 0

    def get(self, url, headers):
        self.calls += 1
        transport = self

        class Response:
            status = 429 if transport.outcome == 'http_error' else 200

            async def __aenter__(self):
                if transport.outcome == 'cancel_headers':
                    transport.waiting.set()
                    await asyncio.Future()
                if transport.outcome == 'connect_error':
                    raise OSError('controlled connection failure')
                if transport.outcome == 'timeout':
                    raise asyncio.TimeoutError('controlled timeout')
                return self

            async def json(self):
                if transport.outcome == 'cancel_body':
                    transport.waiting.set()
                    await asyncio.Future()
                if transport.outcome == 'json_error':
                    raise ValueError('controlled invalid response JSON')
                return {'data': [{'id': 'exact/model'}]}

            async def __aexit__(self, *error):
                transport.closed += 1

        return Response()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', [
    'cancel_headers', 'cancel_body', 'connect_error', 'timeout', 'json_error',
    'http_error', 'success',
])
async def test_discovery_never_reserves_inference_keys_and_recovers(monkeypatch, tmp_path, outcome):
    pool, releases = _real_pool(monkeypatch)
    transport = _Transport(outcome)

    async def get_agent():
        return transport

    route, cache = _load_discovery(pool, get_agent, tmp_path)
    if outcome.startswith('cancel'):
        task = asyncio.create_task(route(SimpleNamespace()))
        await asyncio.wait_for(transport.waiting.wait(), timeout=1)
        assert pool.keys[0].in_flight == pool._in_flight_total == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        response = await route(SimpleNamespace())
        assert response.status_code == 200

    assert pool.keys[0].in_flight == pool._in_flight_total == 0
    assert releases == []
    assert transport.closed == int(outcome not in ('cancel_headers', 'connect_error', 'timeout'))
    transport.outcome = 'success'
    cache._last_attempt = None  # Advance beyond failure backoff without sleeping.
    recovered = await route(SimpleNamespace())
    assert json.loads(recovered.body)['data'][0]['id'] == 'exact/model'
    assert transport.calls == (1 if outcome == 'success' else 2)
    assert releases == []
    await cache.close()


@pytest.mark.asyncio
async def test_public_catalog_works_with_empty_inference_pool(monkeypatch, tmp_path):
    pool, releases = _real_pool(monkeypatch)
    pool.keys = []
    transport = _Transport('success')

    async def get_agent():
        return transport

    route, cache = _load_discovery(pool, get_agent, tmp_path)
    response = await route(SimpleNamespace())
    assert json.loads(response.body)['data'][0]['id'] == 'exact/model'
    assert transport.calls == 1
    assert releases == []
    assert pool._in_flight_total == 0
    await cache.close()
