"""wrapper-coarena contract tests — WRAPPER_CONTRACT.md v3.2 clauses.

Hermetic: no network, no .env, isolated env, isolated snapshot paths.
Covers §2.1 surfaces, §4 input handling, §5 auth, §6.3 store bounds,
§3.5 terminator/cleanup semantics and §10 metrics persistence.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from common.sse import IDLE, iter_chunks_with_idle  # noqa: E402
from common.translations import is_retriable_status, parse_retry_after, should_cooldown_key  # noqa: E402
from common.compat import responses_chat_fallback_allowed  # noqa: E402
from common.model_state import classify_upstream_error  # noqa: E402

REQUIRED_SURFACES = {
    ('POST', '/v1/chat/completions'),
    ('POST', '/v1/responses'),
    ('POST', '/v1/messages'),
    ('POST', '/v1/messages/count_tokens'),
    ('POST', '/v1/completions'),
    ('POST', '/v1/embeddings'),
    ('POST', '/v1/feedback'),
    ('GET', '/v1/models'),
    ('GET', '/api/tags'),
    ('GET', '/v1/capabilities'),
    ('GET', '/health'),
    ('GET', '/ready'),
    ('GET', '/metrics'),
    ('GET', '/metrics/prom'),
    ('GET', '/metrics/model-status'),
    ('GET', '/metrics/models'),
    ('GET', '/metrics/keys'),
    ('GET', '/metrics/activity'),
    ('GET', '/metrics/chart/hourly'),
    ('GET', '/stats'),
    ('GET', '/dashboard'),
    ('GET', '/version'),
    ('GET', '/v1/limits'),
    ('GET', '/v1/usage'),
}


def _load(path, names, **globals_):
    import ast
    import logging
    import random
    import time as _time
    from common.sse import ClosingStreamingResponse, close_stream
    tree = ast.parse(Path(path).read_text())
    nodes = [node for node in tree.body if getattr(node, 'name', None) in names]
    assert len(nodes) == len(names), f'missing {names - {getattr(n, "name", "") for n in nodes}} in {path}'
    for node in nodes:
        node.decorator_list = []
    namespace = dict(asyncio=asyncio, json=json, os=os, random=random, time=_time,
                     logger=logging.getLogger('coarena-test'), aiohttp=__import__('aiohttp'),
                     JSONResponse=JSONResponse, Response=object,
                     StreamingResponse=ClosingStreamingResponse, close_stream=close_stream)
    namespace.update(globals_)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace


def _app(monkeypatch, tmp_path, *, token='test-token'):
    """Import the coarena app with isolated env/state and return the TestClient."""
    global _async_record

    async def _async_record(*args, **kwargs):
        return None

    monkeypatch.setenv('WRAPPER_SKIP_DOTENV', 'true')
    monkeypatch.setenv('BEARER_TOKEN', token)
    monkeypatch.setenv('COARENA_API_KEY_1', 'mock-key-0000000001')
    monkeypatch.setenv('COARENA_API_KEY_2', 'mock-key-0000000002')
    monkeypatch.setenv('COARENA_BASE_URL', 'http://mock.invalid/v1')
    monkeypatch.setenv('METRICS_SNAPSHOT_FILE', str(tmp_path / 'metrics-snapshot.json'))
    monkeypatch.setenv('MODEL_STATE_DB', str(tmp_path / 'model-state.db'))
    monkeypatch.setenv('RATE_LIMIT_RPM', '0')
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    monkeypatch.delenv('COMPATIBILITY_LAYER', raising=False)
    monkeypatch.delenv('RESPONSES_UPSTREAM_MODE', raising=False)
    monkeypatch.delenv('NATIVE_MESSAGES_MODE', raising=False)
    monkeypatch.delenv('NATIVE_PROTOCOL_VALIDATION', raising=False)
    for mod in list(sys.modules):
        if mod.startswith('coarena'):
            del sys.modules[mod]
    from coarena.src import main as coarena_main
    return coarena_main


def test_required_surfaces_registered():
    from coarena.src import main as m
    routes = {(method, route.path) for route in m.app.routes
              for method in getattr(route, 'methods', set())}
    missing = {surface for surface in REQUIRED_SURFACES
               if surface not in routes and not (surface[1].startswith('/v1/models/')
                                                 or (surface[0] == 'GET' and surface[1] == '/v1/models/{model_id:path}'))}
    assert not missing, f'contract §2.1 surfaces missing: {missing}'


def test_embeddings_returns_shaped_501_not_implemented(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    with TestClient(m.app, raise_server_exceptions=False) as client:
        r = client.post('/v1/embeddings', json={'model': 'x', 'input': 'hi'},
                        headers={'Authorization': 'Bearer test-token'})
    assert r.status_code == 501
    err = r.json()['error']
    assert err['type'] == 'not_implemented_error'
    assert 'nvidia-python' in err['message'] or 'openrouter' in err['message']


def test_auth_fails_closed_without_token(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path, token='')
    assert (os.environ.get('BEARER_TOKEN') or '') == ''
    with TestClient(m.app, raise_server_exceptions=False) as client:
        r = client.post('/v1/chat/completions', json={'model': 'm', 'messages': []})
        assert r.status_code == 503, 'contract §5.1: fail closed when no token configured'
        r2 = client.get('/health')
        assert r2.status_code == 200, 'health stays public'


def test_auth_accepts_bearer_and_x_api_key_and_rejects_wrong_token(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    with TestClient(m.app, raise_server_exceptions=False) as client:
        ok_bearer = client.post('/v1/feedback', json={'request_id': 'r', 'rating': 'up'},
                                headers={'Authorization': 'Bearer test-token'})
        ok_apikey = client.post('/v1/feedback', json={'request_id': 'r', 'rating': 'up'},
                                headers={'x-api-key': 'test-token'})
        bad = client.post('/v1/feedback', json={'request_id': 'r', 'rating': 'up'},
                          headers={'Authorization': 'Bearer wrong-token'})
    assert ok_bearer.status_code == ok_apikey.status_code == 429
    # 429 = reached the proxy pool loop (mock.invalid unreachable) — authentication passed.
    assert bad.status_code == 401, 'wrong token must be a clean 401'
    # B-30 parity: non-ASCII tokens compare byte-safely (no TypeError -> 500).
    from common.auth import tokens_match
    assert tokens_match('tokeñ-ünicode', 'server-token') is False
    assert tokens_match('match', 'match') is True


def test_public_paths_exact_match_and_method_gated(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    with TestClient(m.app, raise_server_exceptions=False) as client:
        # Public GETs never require auth; with an unreachable upstream the
        # proxy surface may return the contract exhaustion status (429) —
        # the point is that it is NOT 401/403/503 (no auth required).
        assert client.get('/v1/models').status_code not in (401, 403, 503)
        assert client.get('/version').status_code == 200
        assert client.get('/metrics').status_code == 200
        # POST to a GET-only public path stays authenticated (B-27).
        r = client.post('/v1/models', json={}, headers={'Authorization': 'Bearer wrong'})
        assert r.status_code == 401
        # Unauthenticated POST surfaces are protected (B-31); with a configured
        # server token a missing client credential is a clean 401.
        assert client.post('/v1/chat/completions', json={'model': 'm'}).status_code == 401
        assert client.post('/v1/feedback', json={'x': 1}).status_code == 401


def test_malformed_and_non_object_bodies_yield_shaped_4xx(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    with TestClient(m.app, raise_server_exceptions=False) as client:
        for raw in (b'{not json', b'', b'[1,2,3]', b'"str"', b'42'):
            r = client.post('/v1/chat/completions', data=raw,
                            headers={'Authorization': 'Bearer test-token',
                                     'Content-Type': 'application/json'})
            assert 400 <= r.status_code < 500, f'R-01: {raw!r} must not 5xx'
            assert 'error' in r.json()


def test_max_tokens_adapter_validation(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    monkeypatch.setenv('COMPATIBILITY_LAYER', '2')
    with TestClient(m.app, raise_server_exceptions=False) as client:
        for mt in (0, -5, 'many', True, 1_000_001):
            r = client.post('/v1/chat/completions',
                            json={'model': 'm', 'max_tokens': mt, 'messages': []},
                            headers={'Authorization': 'Bearer test-token'})
            assert r.status_code == 400, f'max_tokens={mt!r} must be a shaped 400 (§4)'
        r = client.post('/v1/chat/completions',
                        json={'model': 'm', 'max_tokens': 1_000_000, 'messages': []},
                        headers={'Authorization': 'Bearer test-token'})
        assert r.status_code in (429, 502), 'valid boundary value passes validation (reaches upstream pool; unreachable upstream yields the contract exhaustion/transport status)'


def test_error_response_counts_metrics(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    before = m.metrics.errors
    m._error_response({'error': {'message': 'x'}}, status_code=429)
    assert m.metrics.errors == before + 1, 'B-39: local error responses increment the counter'
    assert m.metrics.requests >= m.metrics.errors


def test_response_store_is_bounded_on_all_three_axes(monkeypatch, tmp_path):
    m = _app(monkeypatch, tmp_path)
    monkeypatch.setattr(m, '_RESPONSE_STORE_MAX_ENTRIES', 5)
    monkeypatch.setattr(m, '_RESPONSE_STORE_TTL_SEC', 60)
    big = 'x' * 4096
    for i in range(12):
        m._store_response('tenant-a', f'resp-{i}', [{'role': 'user', 'content': big}])
    assert len(m._RESPONSE_STORE) <= 5, 'entry-count bound'
    m._store_response('tenant-b', 'resp-big', [{'role': 'user', 'content': 'y' * 40000}])
    total = sum(entry[1] for entry in m._RESPONSE_STORE.values())
    assert total <= m._RESPONSE_STORE_MAX_BYTES, 'byte bound (B-33)'
    stale = ('tenant-a', 'stale')
    key = m._response_store_key(*stale)
    m._RESPONSE_STORE[key] = (0.0, 10, [{'old': True}])
    assert m._get_stored_conversation(*stale) == [], 'TTL bound'


def test_proxy_request_preserves_native_stream_and_terminators(monkeypatch, tmp_path):
    """§3.5: native passthrough must not synthesize [DONE]; terminal_done may."""
    m = _app(monkeypatch, tmp_path)
    async def scenario():
        for wire in (b'data: partial', b'data: {"x":1}\n\ndata: [DONE]', b'event: future\ndata: {}\n\n'):
            peer = SimpleNamespace(
                status=200,
                content=SimpleNamespace(),
                release=lambda *a: None,
                headers={},
            )
            peer.content = _Content([wire])
            sent = []

            async def request(*args, **kwargs):
                return peer

            kp_ns = _load(ROOT / 'coarena/src/key_pool.py', {'KeyEntry', 'KeyPool'})
            pool = kp_ns['KeyPool']()
            pool.keys = [kp_ns['KeyEntry']('first', 'mock-key-0000000001'),
                         kp_ns['KeyEntry']('second', 'mock-key-0000000002')]
            ns = _load(ROOT / 'coarena/src/main.py',
                       {'_proxy_request', '_usage_tokens', '_sniff_stream_usage'},
                       MODEL_REGISTRY=SimpleNamespace(call_plan=lambda model, surface: SimpleNamespace(
                           model=SimpleNamespace(provider_model_id=model))),
                       same_provider_model_id=lambda *args: True,
                       pool=pool, COARENA_BASE='http://mock.invalid/v1',
                       get_agent=SimpleNamespace, request=request,
                       _build_forward_headers=lambda headers: dict(headers),
                       _parse_retry_after=parse_retry_after, _is_retriable_status=is_retriable_status,
                       _should_cooldown_key=should_cooldown_key,
                       classify_upstream_error=classify_upstream_error,
                       _responses_chat_fallback_allowed=responses_chat_fallback_allowed,
                       _spawn_background=lambda coro, label='bg': (coro.close() if coro else None),
                       _record_model_result=lambda *a, **k: None,
                       metrics=SimpleNamespace(record_request=_async_record, record_tokens=_async_record),
                       _iter_chunks_with_idle=iter_chunks_with_idle, _IDLE=IDLE, HEARTBEAT_MS=100000,
                       _error_response=lambda content, status_code=500, **kwargs:
                           JSONResponse(content, status_code=status_code))
            # make get_agent awaitable
            async def get_agent():
                return SimpleNamespace(request=request)
            ns['get_agent'] = get_agent
            result = await ns['_proxy_request']('POST', 'chat/completions',
                                                {'model': 'native/model', 'stream': True},
                                                stream=True, terminal_done=False)
            body = b''.join([chunk async for chunk in result.body_iterator])
            assert b'[DONE]' in wire or b'[DONE]' not in body, \
                'native passthrough must not synthesize a terminator the upstream did not send'
    asyncio.run(scenario())


class _Content:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        async def _iterate():
            for chunk in self._chunks:
                yield chunk
        return _iterate()


def test_key_pool_defaults_respect_documented_upstream_limit():
    """Upstream documents 30 req/min per key; pool defaults stay at/below it."""
    import time as _time
    ns = _load(ROOT / 'coarena/src/key_pool.py', {'KeyEntry', 'KeyPool'})
    entry = ns['KeyEntry']('key1', 'coa_' + '0' * 32)
    assert entry.soft_rpm <= 30 and entry.hard_rpm <= 30
    assert entry.is_hard_blocked() is False, 'B-37: predicate is side-effect-free'
    assert entry.is_model_blocked('m') is False
    entry.block_model('m', 30, 'rate_limit')
    assert entry.is_model_blocked('m') is True
    # Expiry is an explicit sweep and only clears ELAPSED blocks (B-37).
    entry.model_blocked_until['m'] = _time.time() - 1
    entry.expire_model_blocks()
    assert entry.is_model_blocked('m') is False


def test_systemd_unit_uses_contract_entrypoint():
    unit = (ROOT / 'coarena/systemd/wrapper-coarena.service').read_text()
    assert 'uvicorn src.main:app' in unit, 'contract §1.1: src.main:app entry point'
    assert 'WorkingDirectory=/root/wrapper/coarena' in unit
    assert 'Environment=PYTHONPATH=/root/wrapper/coarena' in unit


def test_env_example_has_no_real_credential():
    example = (ROOT / 'coarena/.env.example').read_text()
    assert 'coa_replace_me' in example
    for line in example.splitlines():
        value = line.split('=', 1)[1].strip() if '=' in line and not line.strip().startswith('#') else ''
        assert not value.startswith('coa_9'), 'a real committed credential pattern must not return'
        assert len(value) < 80 or value == '', 'placeholder values only'


def test_validate_config_fails_fast_on_invalid_compat_layer(monkeypatch, capsys):
    m = _app(monkeypatch, Path(tempfile.mkdtemp()))
    monkeypatch.setenv('COMPATIBILITY_LAYER', '9')
    with pytest.raises(SystemExit):
        m.validate_config()
    monkeypatch.setenv('COMPATIBILITY_LAYER', '1')
    monkeypatch.setenv('LISTEN_PORT', 'not-a-port')
    with pytest.raises(SystemExit):
        m.validate_config()
