"""Execute the production Nous transport with a recording credential-free peer."""
import ast
import asyncio
import json
import logging
import os
from pathlib import Path

import aiohttp
import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAGS = ['product=wrapper-nous', 'client=wrapper-nous', 'user=wrapper-nous']


class RecordingReply:
    status = 200
    headers = {}

    def release(self):
        pass

    async def text(self):
        return json.dumps({'id': 'unchanged', 'opaque': {'future': [None, False]}})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __await__(self):
        async def ready():
            return self
        return ready().__await__()


def transport():
    path = ROOT / 'nous/src/main.py'
    names = {'_prepare_nous_request_metadata', '_UpstreamJSON', 'post_nous'}
    nodes = [node for node in ast.parse(path.read_text()).body if getattr(node, 'name', None) in names]
    assert {node.name for node in nodes} == names
    records = []

    class Session:
        def post(self, url, **kwargs):
            records.append({'url': url, **kwargs})
            return RecordingReply()

    async def get_session():
        return Session()

    namespace = {'os': os, 'json': json, 'aiohttp': aiohttp, 'asyncio': asyncio,
                 'NOUS_BASE': 'http://credential-free.invalid', 'get_session': get_session,
                 'CONNECT_TIMEOUT_SEC': 30, 'STREAM_SOCK_READ_TIMEOUT_SEC': 300,
                 'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['post_nous'], records


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('path', ['v1/chat/completions', 'v1/responses', 'v1/messages'])
async def test_missing_tags_are_supplied_to_actual_transport_without_mutating_client_body(monkeypatch, path, stream):
    monkeypatch.delenv('NOUS_REQUEST_TAGS', raising=False)
    post, records = transport()
    body = {'model': 'client-selected-exact-model', 'opaque_future_request': {'nested': [None, False, '界']}}
    snapshot = json.loads(json.dumps(body))
    status, _ = await post(body, 'dummy-credential', stream=stream, path=path)
    assert status == 200
    assert body == snapshot
    assert records[0]['json'] == {**body, 'tags': DEFAULT_TAGS}
    assert records[0]['url'].endswith('/' + path)


@pytest.mark.parametrize('tags', [[], None, ['client=caller', 'custom=界'], 'intentional-invalid-client-value'])
@pytest.mark.parametrize('stream', [False, True])
async def test_explicit_tags_remain_exact_including_empty_and_invalid_values(monkeypatch, tags, stream):
    monkeypatch.setenv('NOUS_REQUEST_TAGS', 'product=operator-override')
    post, records = transport()
    body = {'model': 'exact-model', 'tags': tags, 'input': [{'type': 'future_input', 'value': True}]}
    await post(body, 'dummy-credential', stream=stream, path='v1/responses')
    assert records[0]['json'] == body


@pytest.mark.parametrize('configured, expected', [
    ('product=operator, client=custom, ,user=service', ['product=operator', 'client=custom', 'user=service']),
    ('', []),
])
async def test_operator_csv_override_is_applied_by_transport(monkeypatch, configured, expected):
    monkeypatch.setenv('NOUS_REQUEST_TAGS', configured)
    post, records = transport()
    await post({'model': 'exact-model'}, 'dummy-credential')
    assert records[0]['json']['tags'] == expected
