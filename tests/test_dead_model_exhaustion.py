"""R-DEAD3 + R-TMO1 reservation-leak regression tests (2026-10-15 line review).

Covers the two findings on top of commit 44ad5f5:
1. The wallclock 504 return in proxy_openai must run BEFORE pool.acquire() —
   the old order leaked one key.in_flight reservation per wallclock event
   (acquire() increments, release_attempt() never runs on that path).
2. A retry loop that exhausts every key with account-scoped 404 misses must
   return the clean local 404 (same shape as the R-DEAD1 fast-fail), not the
   raw upstream JSON:API body. Mixed failures (401/429/timeout + 404) keep
   the upstream error verbatim — partial evidence must not fake a verdict.
3. The catch-all surface (which serves /v1/responses, the Codex path, in
   native-upstream auto mode) gets the same fast-fail gate as proxy_openai,
   so a dead model cannot burn a full key rotation per harness retry there.

AST loading and private package imports avoid dotenv, startup and production
state. The upstream peers and credentials are entirely synthetic.
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
from test_active_native_passthrough import _Upstream, _collect

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'nvidia-python/src/main.py'
MODEL = 'nvidia/cosmos-reason2-8b'

# Raw NVIDIA JSON:API shape — exactly what a not-deployed upstream returns.
DEAD = {'status': 404, 'title': 'Not Found',
        'detail': "Function 'cosmos-reason2-8b': Not found for account 'JWZ1'"}
INVALID = {'error': {'message': 'Invalid API key', 'type': 'invalid_request_error'}}


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


POOL = _module('nvidia_dead_fixture.key_pool', ROOT / 'nvidia-python/src/key_pool.py')


class Peer(_Upstream):
    async def read(self):
        return json.dumps(self.reply).encode()


def _server(peers, records, *, keys=2, fastfail=None):
    tree = ast.parse(SOURCE.read_text())
    server_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Server')
    methods = {'proxy_openai', '_prepare_proxy_body', '_proxy_post',
               '_handle_catch_all', '_stream_proxy', '_dead_model_404'}
    server_class.body = [node for node in server_class.body if getattr(node, 'name', None) in methods]
    nodes = [server_class]

    async def safe_body(peer):
        return json.loads(await peer.text())

    namespace = {
        'json': json, 'asyncio': asyncio, 'time': time, 'os': os,
        'logger': logging.getLogger(__name__), 'Request': object,
        'JSONResponse': JSONResponse, 'Response': Response, 'StreamingResponse': StreamingResponse,
        'resolve_target_model': lambda model: model, 'is_model_unavailable': lambda model: False,
        'guard_stream_unsupported': lambda *args: None,
        'forward_headers': lambda request: {}, 'sanitize_nvidia_payload': lambda body: None,
        'MAX_RETRIES': 0, 'DEFAULT_PARAMS': {},
        'BASE_LLM': 'http://synthetic.invalid', 'route_upstream': lambda path: 'http://synthetic.invalid',
        '_safe_response_body': safe_body, '_normalize_upstream_error': lambda status, body, model: (status, body),
        'classify_upstream_error': classify_upstream_error,
        'same_provider_model_id': lambda provider, first, second: first == second,
        'MODEL_REGISTRY': SimpleNamespace(call_plan=lambda model, surface: SimpleNamespace(
            model=SimpleNamespace(provider_model_id=model), path='/v1/chat/completions')),
        '_passthrough_anthropic_sse': passthrough_anthropic_sse,
        '_parse_retry_after': lambda value: 30,
        'model_from_path': lambda path: '', '_env_flag': lambda *args: False,
        'PRE_RESPONSE_TIMEOUT_MS': 300000,
    }
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 str(SOURCE), 'exec'), namespace)
    server = namespace['Server']()
    server.pool = POOL.KeyPool()
    server.pool.keys = [POOL.KeyEntry(f'key-{index}', f'synthetic-credential-{index}') for index in range(keys)]
    server.registry = server.metrics = None
    server._in_flight = 0
    server._client_timeout = lambda *args, **kwargs: None
    server._normalize_path = lambda path: path
    server._classify_retry = lambda status, classification: classification['retry_same_model']
    server._log_attempt = lambda *args, **kwargs: None
    server._headers_deadline_sec = lambda *args, **kwargs: None

    async def fastfail_none(model_id):
        return fastfail
    server._fastfail_dead_model_reason = fastfail_none

    async def record(*args):
        pass

    server._record_model_response = record

    def post(url, **kwargs):
        records.append((url, copy.deepcopy(kwargs)))
        peer = peers[len(records) - 1]
        return peer

    def request(method, url, **kwargs):
        return post(url, **kwargs)

    server._session = SimpleNamespace(post=post, request=request)
    return server, namespace


def _request(body):
    async def raw():
        return json.dumps(body).encode()
    return SimpleNamespace(headers={}, body=raw, method='POST', url=SimpleNamespace(query=''))


def _body(extra=None):
    return {'model': MODEL, 'max_tokens': 64,
            'messages': [{'role': 'user', 'content': 'Synthetic input.'}], **copy.deepcopy(extra or {})}


@pytest.mark.parametrize('stream', [False, True])
async def test_all_account_miss_404s_synthesize_clean_local_404(stream):
    """R-DEAD3: every key answering 404 not-deployed must produce the clean
    local 404 verdict — never the raw JSON:API body — on exhaustion."""
    peers = [Peer(status=404, reply=DEAD) for _ in range(3)]
    records = []
    server, _ = _server(peers, records, keys=3)
    body = _body({'stream': stream})
    response = await server.proxy_openai(body, {}, MODEL, _request(body))
    assert response['status'] == 404
    error = response['data']['error']
    assert error['type'] == 'not_found_error'
    assert error['code'] == 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert '3 distinct credentials just returned 404 Not Found' in error['message']
    assert 'available=true in /v1/models' in error['message']
    assert response['headers'].get('x-wrapper-attempts') == '3'
    assert len(records) == 3  # full rotation, then the synthesized verdict
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)
    assert all(peer.release_count == 1 for peer in peers)


async def test_mixed_401_then_404_keeps_upstream_error_verbatim():
    """R-DEAD3 negative case: partial evidence (a 401 among the 404s) must NOT
    synthesize a dead-model verdict — the raw upstream error is preserved."""
    peers = [Peer(status=401, reply=INVALID), Peer(status=404, reply=DEAD)]
    records = []
    server, _ = _server(peers, records, keys=2)
    body = _body()
    response = await server.proxy_openai(body, {}, MODEL, _request(body))
    assert response['status'] == 404
    assert response['data'] == DEAD  # raw passthrough, no synthesized code
    assert response['data'].get('error', {}).get('code') != 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert len(records) == 2
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_wallclock_504_releases_every_reservation(monkeypatch):
    """R-TMO1 leak fix: the wallclock 504 path must run BEFORE pool.acquire().
    Pre-fix order: iteration 2 acquired a key (in_flight +1) and then returned
    504 without release_attempt() — a permanent reservation leak that skewed
    _pick_key/peek_key load balancing and eventually made keys
    probe-ineligible (in_flight >= 5)."""
    monkeypatch.setenv('REQUEST_RETRY_WALLCLOCK_SEC', '0.000001')
    peers = [Peer(status=404, reply=DEAD), None]  # second peer must never be used
    records = []
    server, _ = _server(peers, records, keys=2)
    body = _body()
    response = await server.proxy_openai(body, {}, MODEL, _request(body))
    assert response['status'] == 504
    assert response['data']['error']['code'] == 'UPSTREAM_WALLCLOCK'
    assert len(records) == 1  # only the first attempt reached the upstream
    # THE regression assertion: no leaked reservation on any key.
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_catch_all_all_404s_synthesize_clean_local_404():
    """R-DEAD3 on the catch-all surface: /v1/responses (Codex) exhaustion
    with all account misses returns the clean local 404."""
    peers = [Peer(status=404, reply=DEAD) for _ in range(2)]
    records = []
    server, _ = _server(peers, records, keys=2)
    body = {'model': MODEL, 'input': 'hi'}
    response = await server._handle_catch_all(_request(body), '/v1/responses')
    assert response.status_code == 404
    error = json.loads(response.body)['error']
    assert error['code'] == 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert '2 distinct credentials just returned 404' in error['message']
    assert len(records) == 2
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_catch_all_fastfail_gate_blocks_before_any_upstream_call():
    """R-DEAD1 gate on the catch-all surface: a fast-fail verdict returns the
    local 404 with ZERO upstream calls (records stay empty)."""
    records = []
    server, _ = _server([], records, keys=2, fastfail='Model X is not deployed on any configured account')
    body = {'model': MODEL, 'input': 'hi'}
    response = await server._handle_catch_all(_request(body), '/v1/responses')
    assert response.status_code == 404
    error = json.loads(response.body)['error']
    assert error['type'] == 'not_found_error'
    assert error['code'] == 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert response.headers.get('x-wrapper-fastfail') == '1'
    assert records == []
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_proxy_post_all_404s_synthesize_clean_local_404():
    """R-DEAD3 on the legacy POST surface: exhaustion with all account misses
    returns the clean local 404 JSONResponse."""
    peers = [Peer(status=404, reply=DEAD) for _ in range(2)]
    records = []
    server, _ = _server(peers, records, keys=2)
    body = _body()
    response = await server._proxy_post(_request(body), body, b'', MODEL, '/v1/embeddings',
                                        lambda key: 'http://synthetic.invalid/v1/embeddings')
    assert response.status_code == 404
    error = json.loads(response.body)['error']
    assert error['code'] == 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert len(records) == 2
    assert server._in_flight == 0 and all(key.in_flight == 0 for key in server.pool.keys)


async def test_dead_model_404_helper_shape_matches_fastfail_contract():
    """The synthesized 404 must be shape-identical to the R-DEAD1 fast-fail:
    OpenAI error envelope, actionable message, not_found_error type."""
    records = []
    server, _ = _server([], records, keys=1)
    verdict = server._dead_model_404(MODEL, 6, 6)
    assert verdict['status'] == 404
    assert verdict['data']['error']['type'] == 'not_found_error'
    assert verdict['data']['error']['code'] == 'MODEL_NOT_DEPLOYED_ON_ANY_ACCOUNT'
    assert '6 distinct credentials just returned 404 Not Found' in verdict['data']['error']['message']
    assert 'raw' not in verdict['data']['error']['message']
    assert verdict['headers'] == {'x-wrapper-attempts': '6'}
