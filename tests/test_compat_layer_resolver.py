"""R17 regression locks — B-17.1: COMPATIBILITY_LAYER=3 (auto-discovery) was
never honored by any wrapper: _is_anthropic_upstream() reads only the env
var, probe results were never consumed, and the probe itself mis-normalised
v1-style base URLs (`{base}/v1/v1/messages` → 404 → default OpenAI).

These tests pin the shared resolver + probe-base normalisation in
common/compat.py against canned sessions (no network)."""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import common.compat as compat  # noqa: E402


class _FakeResp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = body if body is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._body


class _FakeSession:
    """Canned aiohttp-like session: routes map (method, path) -> (status, body)."""

    def __init__(self, routes):
        self.routes = routes

    def _route(self, method, url):
        from urllib.parse import urlparse
        path = urlparse(url).path
        return self.routes.get((method, path), _FakeResp(404))

    def get(self, url, timeout=None):
        return self._route('GET', url)

    def post(self, url, json=None, timeout=None):
        return self._route('POST', url)


ANTHRO_ROUTES = {('POST', '/v1/messages'): _FakeResp(200, {'type': 'message'})}
OPENAI_ROUTES = {
    ('GET', '/v1/models'): _FakeResp(200, {'data': []}),
    ('POST', '/v1/chat/completions'): _FakeResp(200, {'choices': []}),
}


def _run(coro):
    return asyncio.run(coro)


class TestR17ProbeBaseNormalization(unittest.TestCase):
    def setUp(self):
        compat._probe_cache.clear()

    def test_v1_style_base_strips_suffix(self):
        self.assertEqual(compat._probe_base('http://h:1/v1'), 'http://h:1')
        self.assertEqual(compat._probe_base('http://h:1/v1/'), 'http://h:1')
        self.assertEqual(compat._probe_base('http://h:1'), 'http://h:1')
        self.assertEqual(compat._probe_base('http://h:1/api/v2'), 'http://h:1/api/v2')

    def test_v1_style_base_detects_anthropic(self):
        """The B-17.1 case: base WITH /v1 against an Anthropic-only upstream
        must still detect Anthropic (was: /v1/v1/messages → 404 → OpenAI)."""
        sess = _FakeSession(ANTHRO_ROUTES)
        got = _run(compat.probe_upstream_compatibility(sess, 'http://mock:19998/v1'))
        self.assertEqual(got, '2')

    def test_root_style_base_detects_anthropic(self):
        sess = _FakeSession(ANTHRO_ROUTES)
        got = _run(compat.probe_upstream_compatibility(sess, 'http://mock:19998'))
        self.assertEqual(got, '2')

    def test_detects_openai(self):
        sess = _FakeSession(OPENAI_ROUTES)
        got = _run(compat.probe_upstream_compatibility(sess, 'http://mock:19999'))
        self.assertEqual(got, '1')

    def test_unreachable_falls_back_to_openai(self):
        sess = _FakeSession({})  # everything 404
        got = _run(compat.probe_upstream_compatibility(sess, 'http://mock:9'))
        self.assertEqual(got, '1')


class TestR17Resolver(unittest.TestCase):
    def setUp(self):
        compat._probe_cache.clear()
        self._old = os.environ.get('COMPATIBILITY_LAYER')

    def tearDown(self):
        if self._old is None:
            os.environ.pop('COMPATIBILITY_LAYER', None)
        else:
            os.environ['COMPATIBILITY_LAYER'] = self._old
        compat._probe_cache.clear()

    def test_explicit_layers_never_touch_network(self):
        os.environ['COMPATIBILITY_LAYER'] = '1'
        self.assertFalse(_run(compat.resolve_upstream_is_anthropic(None, 'http://x/v1')))
        os.environ['COMPATIBILITY_LAYER'] = '2'
        self.assertTrue(_run(compat.resolve_upstream_is_anthropic(None, 'http://x/v1')))

    def test_auto_layer3_uses_probe(self):
        os.environ['COMPATIBILITY_LAYER'] = '3'
        sess = _FakeSession(ANTHRO_ROUTES)
        self.assertTrue(_run(compat.resolve_upstream_is_anthropic(sess, 'http://mock:19998/v1')))
        compat._probe_cache.clear()
        sess2 = _FakeSession(OPENAI_ROUTES)
        self.assertFalse(_run(compat.resolve_upstream_is_anthropic(sess2, 'http://mock:19999/v1')))

    def test_auto_probe_exception_falls_back_openai(self):
        os.environ['COMPATIBILITY_LAYER'] = '3'

        class _Boom:
            def get(self, url, timeout=None):
                raise RuntimeError('down')

            def post(self, url, json=None, timeout=None):
                raise RuntimeError('down')

        self.assertFalse(_run(compat.resolve_upstream_is_anthropic(_Boom(), 'http://unreachable'))) 

    def test_session_getter_callable(self):
        os.environ['COMPATIBILITY_LAYER'] = '3'
        sess = _FakeSession(ANTHRO_ROUTES)

        async def getter():
            return sess

        self.assertTrue(_run(compat.resolve_upstream_is_anthropic(getter, 'http://mock:19998/v1')))


if __name__ == '__main__':
    unittest.main()
