"""Executable production regressions for auth, replay memory and peer identity."""

import ast
import asyncio
import copy
import json
import os
import time
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Optional

import pytest

from common.auth import (authenticated_client_token, check_auth,
                         extract_client_token, extract_client_tokens, tokens_match)

ROOT = Path(__file__).resolve().parents[1]


def _functions(wrapper, names, **values):
    """Run actual helper definitions without importing provider credentials."""
    source = (ROOT / wrapper / 'src/main.py').read_text()
    tree = ast.parse(source)
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names)
    ns = dict(asyncio=asyncio, copy=copy, json=json, os=os, time=time,
              Dict=Dict, Optional=Optional, Request=object, **values)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / wrapper), 'exec'), ns)
    return ns


@pytest.mark.parametrize('headers', [
    {'authorization': 'Bearer stale', 'x-api-key': 'valid'},
    {'authorization': 'Bearer valid', 'x-api-key': 'stale'},
    {'authorization': 'Basic stale', 'x-api-key': 'valid'},
    {'x-api-key': 'valid'},
    {'authorization': 'bEaReR valid'},
])
def test_auth_accepts_either_valid_sdk_credential(monkeypatch, headers):
    monkeypatch.setenv('BEARER_TOKEN', 'valid')
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    assert check_auth(headers).ok


def test_auth_helper_retains_string_api_and_rejects_all_wrong_tokens(monkeypatch):
    monkeypatch.setenv('BEARER_TOKEN', 'valid')
    monkeypatch.delenv('DISABLE_AUTH', raising=False)
    headers = {'authorization': 'Bearer wrong', 'x-api-key': 'also-wrong'}
    assert extract_client_token(headers) == 'wrong'
    assert extract_client_tokens(headers) == ('wrong', 'also-wrong')
    assert extract_client_token({}) == ''
    assert check_auth(headers).status == 401


def test_principal_uses_the_accepted_token_across_header_styles(monkeypatch):
    monkeypatch.setenv('BEARER_TOKEN', 'valid')
    assert authenticated_client_token({'authorization': 'Bearer stale', 'x-api-key': 'valid'}) == 'valid'
    assert authenticated_client_token({'authorization': 'Bearer valid'}) == 'valid'


async def test_openrouter_middleware_accepts_dual_headers_without_weakening_management(monkeypatch):
    from starlette.datastructures import Headers
    import logging
    import uuid
    monkeypatch.setenv('BEARER_TOKEN', 'valid')
    ns = _functions('openrouter', ['auth_middleware', '_client_ip'],
                    app=SimpleNamespace(middleware=lambda *args: lambda fn: fn),
                    DISABLE_AUTH=False, REQUIRE_AUTH=True, MANAGEMENT_PREFIX='/openrouter/',
                    _is_loopback_client=lambda request: request.client.host == '127.0.0.1',
                    _is_public_path=lambda *args: False,
                    _management_token=lambda: 'management', _bearer_token=lambda: 'valid',
                    _error_response=lambda body, status_code, **kw: SimpleNamespace(status_code=status_code),
                    extract_client_tokens=extract_client_tokens, tokens_match=tokens_match,
                    check_rate_limit=lambda ip: True, logger=logging.getLogger(__name__), uuid=uuid)
    request = SimpleNamespace(method='POST', url=SimpleNamespace(path='/v1/responses'),
                              headers=Headers({'authorization': 'Bearer stale', 'x-api-key': 'valid'}),
                              client=SimpleNamespace(host='127.0.0.1'))
    async def next_handler(request):
        return SimpleNamespace(status_code=200, headers={})
    middleware = ns['auth_middleware']
    assert (await middleware(request, next_handler)).status_code == 200
    request.url.path = '/openrouter/keys/create'
    assert (await middleware(request, next_handler)).status_code == 401
    request.headers = Headers({'x-api-key': 'management'})
    request.client.host = '192.0.2.1'
    assert (await middleware(request, next_handler)).status_code == 403


def _store(wrapper, max_bytes=180, max_entries=2):
    if wrapper == 'nous':
        ns = _functions(wrapper, ['_response_store_key', '_prune_response_store_locked',
                                  'store_conversation', 'get_stored_conversation'],
                        _RESPONSE_STORE={}, _STORE_LOCK=asyncio.Lock(),
                        _RESPONSE_STORE_MAX=max_entries, _RESPONSE_STORE_MAX_BYTES=max_bytes,
                        _RESPONSE_STORE_TTL_SEC=60)
        async def put(principal, rid, data):
            await ns['store_conversation'](principal, rid, data)
        get = ns['get_stored_conversation']
    elif wrapper == 'opencode':
        ns = _functions(wrapper, ['_prune_response_store', '_store_response', '_load_response'],
                        _RESPONSE_STORE={}, _RESPONSE_STORE_MAX_ENTRIES=max_entries,
                        _RESPONSE_STORE_MAX_BYTES=max_bytes, _RESPONSE_STORE_TTL_SEC=60,
                        _RESPONSE_STORE_MAX_CHARS=4000000,
                        _RESPONSE_STORE_MAX_ENTRY_CHARS=500000)
        async def put(principal, rid, data):
            ns['_store_response'](principal, rid, data)
        get = ns['_load_response']
    else:
        ns = _functions(wrapper, ['_response_store_key', '_prune_response_store',
                                  '_store_response', '_get_stored_conversation'],
                        _RESPONSE_STORE=OrderedDict(), _RESPONSE_STORE_MAX_ENTRIES=max_entries,
                        _RESPONSE_STORE_MAX_BYTES=max_bytes, _RESPONSE_STORE_TTL_SEC=60,
                        logger=__import__('logging').getLogger(__name__))
        async def put(principal, rid, data):
            ns['_store_response'](principal, rid, data)
        get = lambda principal, rid: ns['_get_stored_conversation'](principal, rid) or None
    return ns, put, get


@pytest.mark.parametrize('wrapper', ['nous', 'opencode', 'blackbox', 'openrouter'])
async def test_store_caps_utf8_total_and_rejects_one_oversized_history(wrapper):
    ns, put, get = _store(wrapper, max_entries=20)
    history = [{'role': 'user', 'content': '界' * 20}]
    await put('tenant', 'one', history)
    await put('tenant', 'two', history)
    assert get('tenant', 'one') is None  # byte cap reached before entry cap
    assert get('tenant', 'two') == history
    await put('tenant', 'huge', [{'role': 'user', 'content': '界' * 100}])
    assert get('tenant', 'huge') is None
    assert sum(len(json.dumps(entry[2] if isinstance(entry, tuple) else entry['data'],
                              ensure_ascii=False).encode('utf-8'))
               for entry in ns['_RESPONSE_STORE'].values()) <= 180


@pytest.mark.parametrize('wrapper', ['nous', 'opencode', 'blackbox', 'openrouter'])
async def test_store_expires_on_read_and_isolates_tenants_and_mutations(wrapper, monkeypatch):
    ns, put, get = _store(wrapper, max_bytes=10000)
    monkeypatch.setattr(time, 'time', lambda: 1000.0)
    history = [{'role': 'user', 'content': 'original'}]
    await put('tenant-a', 'id', history)
    history[0]['content'] = 'mutated'
    assert get('tenant-b', 'id') is None
    loaded = get('tenant-a', 'id')
    assert loaded[0]['content'] == 'original'
    loaded[0]['content'] = 'mutated-copy'
    assert get('tenant-a', 'id')[0]['content'] == 'original'
    monkeypatch.setattr(time, 'time', lambda: 1061.0)
    assert get('tenant-a', 'id') is None
    assert ns['_RESPONSE_STORE'] == {}


@pytest.mark.parametrize('wrapper', ['nous', 'opencode', 'blackbox', 'openrouter'])
async def test_store_evicts_oldest_at_entry_cap(wrapper):
    ns, put, get = _store(wrapper, max_bytes=10000)
    for rid in ('one', 'two', 'three'):
        await put('tenant', rid, [{'role': 'user', 'content': rid}])
    assert len(ns['_RESPONSE_STORE']) == 2
    assert get('tenant', 'one') is None
    assert get('tenant', 'three')[0]['content'] == 'three'


@pytest.mark.parametrize('wrapper', ['opencode', 'blackbox', 'openrouter'])
def test_rate_limit_identity_never_uses_untrusted_forwarded_header(wrapper):
    client_ip = _functions(wrapper, ['_client_ip'])['_client_ip']
    request = SimpleNamespace(client=SimpleNamespace(host='192.0.2.1'),
                              headers={'x-forwarded-for': '198.51.100.1'})
    assert client_ip(request) == '192.0.2.1'
    request.client = None
    assert client_ip(request) == 'unknown'
    request.headers['x-forwarded-for'] = '198.51.100.2'
    assert client_ip(request) == 'unknown'


async def test_nvidia_rejects_large_body_before_json_guard_buffers_it(monkeypatch):
    import sys
    from contextlib import asynccontextmanager
    from types import ModuleType
    import httpx
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from common.body_guard import JSONBodyGuard
    from common.middleware import RequestSizeLimiter

    guard_calls = []
    original_guard = JSONBodyGuard.__call__
    async def observe_guard(self, scope, receive, send):
        guard_calls.append(scope['path'])
        await original_guard(self, scope, receive, send)
    monkeypatch.setattr(JSONBodyGuard, '__call__', observe_guard)

    class SmallLimiter(RequestSizeLimiter):
        def __init__(self, app):
            super().__init__(app, max_bytes=64)

    class NoProviderServer:
        def __init__(self, app):
            self.app = app
        def _register_routes(self):
            self.app.post('/test')(lambda: {})

    catalog = ModuleType('common.catalog_integration')
    catalog.setup_catalog_routes = lambda app: None
    catalog.setup_mcp_server = lambda app, name: None
    catalog.free_only_enabled = lambda: False
    monkeypatch.setitem(sys.modules, 'common.catalog_integration', catalog)
    ns = _functions('nvidia-python', ['create_app'], FastAPI=FastAPI,
                    CORSMiddleware=CORSMiddleware, _HAS_SIZE_LIMITER=True,
                    RequestSizeLimiter=SmallLimiter, Server=NoProviderServer,
                    asynccontextmanager=asynccontextmanager)
    app = ns['create_app']()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/test', json={'content': 'x' * 100})
    assert response.status_code == 413
    assert guard_calls == []
