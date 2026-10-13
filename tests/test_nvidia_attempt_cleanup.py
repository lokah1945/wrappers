"""Cancellation before headers must release each native NVIDIA reservation."""
import ast
import asyncio
import json
import logging
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse, Response
from common.sse import ClosingStreamingResponse as StreamingResponse


def load_server():
    path = Path(__file__).resolve().parents[1] / 'nvidia-python/src/main.py'
    tree = ast.parse(path.read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
    names = {'proxy_openai', '_compat2_proxy', '_proxy_post', '_handle_catch_all'}
    original.body = [node for node in original.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    namespace = {
        'Request': object, 'json': json, 'time': time, 'asyncio': asyncio,
        'logger': logging.getLogger(__name__), 'JSONResponse': JSONResponse,
        'Response': Response, 'StreamingResponse': StreamingResponse,
        'BASE_LLM': 'http://controlled.invalid', 'MAX_RETRIES': 0,
        'DEFAULT_PARAMS': {}, 'sanitize_nvidia_payload': lambda body: None,
        'is_model_unavailable': lambda model: False,
        'guard_stream_unsupported': lambda body, model: None,
        'same_provider_model_id': lambda provider, first, second: first == second,
        'MODEL_REGISTRY': SimpleNamespace(call_plan=lambda model, surface: SimpleNamespace(
            model=SimpleNamespace(provider_model_id=model), path='/v1/chat/completions')),
        'route_upstream': lambda path: 'http://controlled.invalid',
        'resolve_target_model': lambda model: model,
        'model_from_path': lambda path: '', 'forward_headers': lambda request: {},
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[original], type_ignores=[])), str(path), 'exec'), namespace)
    return namespace['Server']


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['proxy_openai', '_compat2_proxy', '_proxy_post', '_handle_catch_all'])
async def test_cancel_during_header_wait_releases_once(method):
    key = SimpleNamespace(api_key='synthetic-credential', in_flight=0, releases=0)

    def release():
        key.in_flight -= 1
        key.releases += 1

    key.decrement_in_flight = release

    async def acquire(model, exclude=None):
        key.in_flight += 1
        return {'key': key}

    waiting = asyncio.Event()

    async def post(*args, **kwargs):
        waiting.set()
        await asyncio.Future()

    server = load_server()()
    server.pool = SimpleNamespace(total_keys=1, acquire=acquire)
    server._session = SimpleNamespace(post=post, request=post)
    server._in_flight = 0
    server.metrics = None
    server._client_timeout = lambda *args: None
    server._normalize_path = lambda path: path

    async def prepare(body, model, metric_path=None):
        return dict(body)

    server._prepare_proxy_body = prepare
    body = {'model': 'exact/model', 'messages': [{'role': 'user', 'content': 'test'}]}

    async def request_body():
        return json.dumps(body).encode()

    request = SimpleNamespace(method='POST', headers={}, url=SimpleNamespace(query=''), body=request_body)
    if method == 'proxy_openai':
        call = server.proxy_openai(body, {}, body['model'], request)
    elif method == '_compat2_proxy':
        call = server._compat2_proxy(body, request)
    elif method == '_proxy_post':
        call = server._proxy_post(request, body, b'', body['model'], '/v1/embeddings', lambda selected: 'http://controlled.invalid')
    else:
        call = server._handle_catch_all(request, '/v1/responses')
    task = asyncio.create_task(call)
    await asyncio.wait_for(waiting.wait(), timeout=1)
    assert key.in_flight == server._in_flight == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert key.in_flight == server._in_flight == 0
    assert key.releases == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', ['empty', 'key', 'model', 'attempted', 'saturated'])
async def test_no_eligible_key_returns_without_cooldown_wait(blocked):
    path = Path(__file__).resolve().parents[1] / 'nvidia-python/src/key_pool.py'
    spec = importlib.util.spec_from_file_location('isolated_nvidia_key_pool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pool = module.KeyPool()
    key = module.KeyEntry('synthetic', 'synthetic-credential')
    if blocked != 'empty':
        pool.keys = [key]
    if blocked == 'key':
        key.hard_blocked_until = time.time() + 3600
    elif blocked == 'model':
        key.model_blocks['exact/model'] = time.time() + 3600
    elif blocked == 'saturated':
        pool._key_model_limit[f'{key.label}/exact/model'] = 1
        pool.record_model('exact/model', key.label)
    excluded = {key.api_key} if blocked == 'attempted' else set()
    result = await asyncio.wait_for(pool.acquire('exact/model', exclude=excluded), timeout=0.2)
    assert result is None
    assert key.in_flight == 0
    assert pool._waiting == {}
    assert not pool._lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,error,fallback', [
    (200, {'object': 'response', 'opaque': {'future': True}}, False),
    (404, {'detail': 'Not Found'}, True),
    (405, {'error': {'message': 'method unsupported'}}, True),
    (501, {'error': {'message': 'endpoint not implemented'}}, True),
    (404, {'error': {'message': 'model not found', 'code': 'model_not_found'}}, False),
    (400, {'error': {'message': 'missing tags'}}, False),
    (401, {'error': {'message': 'invalid credential'}}, False),
    (429, {'error': {'message': 'quota exceeded'}}, False),
    (503, {'error': {'message': 'unavailable'}}, False),
])
async def test_responses_prefers_native_and_only_adapts_unsupported_route(monkeypatch, status, error, fallback):
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('RESPONSES_UPSTREAM_MODE', 'auto')
    path = Path(__file__).resolve().parents[1] / 'nvidia-python/src/main.py'
    tree = ast.parse(path.read_text())
    route = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == 'responses_api')
    route.decorator_list = []
    counts = {'native': 0, 'adapter': 0}

    async def native(request, path):
        counts['native'] += 1
        return JSONResponse(error, status_code=status)

    async def adapter(request, raw):
        counts['adapter'] += 1
        return {'id': 'resp_adapter', 'object': 'response', 'output': []}, None, 200

    server = SimpleNamespace(_handle_catch_all=native,
                             responses_handler=SimpleNamespace(handle_responses_api=adapter))
    namespace = {'self': server, 'Request': object, 'json': json,
                 'logger': logging.getLogger(__name__), 'logging': logging,
                 'JSONResponse': JSONResponse, 'StreamingResponse': StreamingResponse,
                 '_is_anthropic_upstream': lambda: False}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), str(path), 'exec'), namespace)

    async def body():
        return b'{"model":"exact/model","input":"test","opaque_future":true}'

    reply = await namespace['responses_api'](SimpleNamespace(body=body))
    assert counts == {'native': 1, 'adapter': int(fallback)}
    assert reply.status_code == (200 if fallback else status)
    assert json.loads(reply.body) == ({'id': 'resp_adapter', 'object': 'response', 'output': []}
                                    if fallback else error)
