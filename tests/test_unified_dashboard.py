"""Isolated gateway checks; pending providers are fake metadata, never implementations."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import aiohttp
import httpx
import pytest
from starlette.requests import Request
from starlette.requests import ClientDisconnect

from dashboard.app import MAX_BODY, bridge_html, create_app, safe_numbers

pytestmark = pytest.mark.scope_policy


class Content:
    def __init__(self, body=b"", *, wait=False):
        self.body = body
        self.wait = wait

    async def iter_chunked(self, size):
        yield self.body
        if self.wait:
            await asyncio.Event().wait()


class Upstream:
    def __init__(self, body=None, status=200, content_type="application/json", *, wait=False):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = Content(body if isinstance(body, bytes) else json.dumps(body or {}).encode(), wait=wait)
        self.closed = False
        self.close_count = 0

    def close(self):
        self.close_count += 1
        self.closed = True


class Session:
    def __init__(self, responses, **kwargs):
        self.responses = responses
        self.calls = []
        self.connector = kwargs.get("connector")
        self.closed = False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parts = urlsplit(url)
        response = self.responses.get((parts.port, parts.path), Upstream(status=404))
        if isinstance(response, Exception):
            raise response
        return response() if callable(response) else response

    async def close(self):
        self.closed = True
        if self.connector is not None:
            await self.connector.close()


@pytest.fixture
def repo(tmp_path):
    entries = {
        "alpha": {"status": "production", "port": 19811, "upstream": "Alpha", "supports": ["openai_chat"]},
        "beta": {"status": "production", "port": 19812, "upstream": "Beta"},
        "blackbox": {"status": "pending", "port": 19813, "upstream": "Frozen one"},
        "opencode": {"status": "pending", "port": 19814, "upstream": "Frozen two"},
    }
    (tmp_path / "wrappers.json").write_text(json.dumps({"wrappers": entries}))
    return tmp_path


@asynccontextmanager
async def gateway(repo, responses=None, *, session_type=Session):
    holder = {}

    def factory(**kwargs):
        holder["session"] = session_type(responses or {}, **kwargs)
        return holder["session"]

    app = create_app(repo, session_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard") as client:
            yield app, client, holder["session"]
    assert holder["session"].closed


async def test_home_and_dashboard_service_readiness(repo):
    async with gateway(repo) as (_, client, session):
        home = await client.get("/")
        assert home.status_code == 200
        assert 'id="wrapper-select"' in home.text
        assert 'id="wrapper-frame"' in home.text
        assert '<header class="topbar">' in home.text
        assert (await client.get("/ready")).json() == {"ready": True, "service": "wrapper-dashboard", "scope": "dashboard_service"}
        assert (await client.get("/health")).json()["read_only"] is True
        assert session.calls == []


async def test_global_status_preserves_offline_and_unknown_without_secrets(repo):
    responses = {
        (19811, "/health"): Upstream({"status": "ok", "keys": 3, "available": 2, "live_keys": [{"token": "PRIVATE_TOKEN"}], "secret": "PRIVATE_TOKEN", "source_root": "private_path"}),
        (19811, "/ready"): Upstream({"error": "PRIVATE_TOKEN"}, status=401),
        (19811, "/stats"): Upstream({"total_requests": 91, "key_pool": {"api_key": "PRIVATE_TOKEN"}}),
        (19812, "/health"): aiohttp.ClientConnectionError("PRIVATE_TOKEN"),
    }
    async with gateway(repo, responses) as (_, client, session):
        response = await client.get("/api/status")
        assert response.status_code == 200
        assert "PRIVATE_TOKEN" not in response.text and "private_path" not in response.text
        data = response.json()
        services = {service["name"]: service for service in data["wrappers"]}
        assert services["alpha"]["status"] == "online"
        assert services["alpha"]["readiness"] == "protected"
        assert services["alpha"]["ready"] is None
        assert services["alpha"]["counters"] == {"keys": 3, "available": 2, "total_requests": 91}
        assert services["beta"]["status"] == "offline"
        assert services["blackbox"]["status"] == "pending"
        assert services["opencode"]["readiness"] == "not_probed"
        assert data["counts"] == {"active": 2, "online": 1, "ready": 0, "pending": 2}
        assert {urlsplit(url).port for url, _ in session.calls} == {19811, 19812}
        call_count = len(session.calls)
        assert (await client.get("/api/status")).json() == data
        assert len(session.calls) == call_count


async def test_global_readiness_false_degrades_service(repo):
    async with gateway(repo, {(19811, "/health"): Upstream({"status": "ok"}),
                              (19811, "/ready"): Upstream({"ready": False})}) as (_, client, _):
        data = (await client.get("/api/status")).json()
        assert data["wrappers"][0]["status"] == "degraded"
        assert data["wrappers"][0]["readiness"] == "not_ready"


@pytest.mark.parametrize("path", ["/wrappers/blackbox/dashboard", "/wrappers/opencode/health", "/wrappers/unknown/dashboard"])
async def test_pending_or_unknown_proxy_rejected_before_transport(repo, path):
    async with gateway(repo) as (_, client, session):
        response = await client.get(path)
        assert response.status_code in (404, 409)
        assert session.calls == []


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/openrouter/keys/list", "/metrics/reset", "/internal/status", "/../health", "/http://elsewhere/health"])
async def test_inference_management_and_unlisted_routes_never_reach_upstream(repo, path):
    async with gateway(repo) as (_, client, session):
        assert (await client.get("/wrappers/alpha" + path)).status_code == 404
        assert session.calls == []


async def test_proxy_forwards_only_browser_credentials_and_safe_query(repo):
    upstream = Upstream({"keys": 2})
    async with gateway(repo, {(19811, "/metrics"): upstream}) as (_, client, session):
        response = await client.get("/wrappers/alpha/metrics?window=24h&token=bad&api_key=bad&auth=bad&secret=bad&limit=3",
                                    headers={"Authorization": "Bearer client-token", "x-api-key": "client-key",
                                             "Cookie": "private-cookie", "X-Forwarded-Host": "evil.example"})
        assert response.status_code == 200
        url, kwargs = session.calls[0]
        assert urlsplit(url).hostname == "127.0.0.1"
        assert parse_qs(urlsplit(url).query) == {"window": ["24h"], "limit": ["3"]}
        assert kwargs["headers"] == {"accept": "*/*", "authorization": "Bearer client-token", "x-api-key": "client-key"}
        assert kwargs["allow_redirects"] is False
        assert upstream.closed
        assert response.headers["cache-control"] == "no-store"


async def test_proxy_write_methods_are_disabled(repo):
    async with gateway(repo) as (_, client, session):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert (await client.request(method, "/wrappers/alpha/metrics")).status_code == 405
        assert session.calls == []


async def test_redirects_and_oversized_responses_close_reservation(repo):
    redirect = Upstream(status=302)
    oversized = Upstream(b"x" * (MAX_BODY + 1))
    async with gateway(repo, {(19811, "/health"): redirect, (19811, "/metrics"): oversized}) as (app, client, _):
        assert (await client.get("/wrappers/alpha/health")).status_code == 502
        assert (await client.get("/wrappers/alpha/metrics")).status_code == 502
        assert redirect.closed and oversized.closed
        assert app.state.slots._value == 8


async def test_html_bridge_precedes_original_script_and_never_injects_credentials(repo):
    original = b'<html><head><script>originalDashboard()</script></head><body>Existing layout</body></html>'
    async with gateway(repo, {(19811, "/dashboard"): Upstream(original, content_type="text/html; charset=utf-8")}) as (_, client, _):
        response = await client.get("/wrappers/alpha/dashboard")
        assert response.status_code == 200
        assert response.text.index("data-unified-dashboard-bridge") < response.text.index("originalDashboard")
        assert 'const prefix = "/wrappers/alpha"' in response.text
        assert "Existing layout" in response.text
        assert "PRIVATE_TOKEN" not in response.text


async def test_stream_bytes_and_disconnect_cleanup_are_transparent(repo):
    wire = b'event: custom\ndata: {"future":"field"}\n\n'
    upstream = Upstream(wire, content_type="text/event-stream", wait=True)
    async with gateway(repo, {(19811, "/events"): upstream}) as (app, _, _):
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/wrappers/{name}/{path:path}")
        request = Request({"type": "http", "method": "GET", "path": "/wrappers/alpha/events", "query_string": b"", "headers": []})
        response = await endpoint("alpha", "events", request)
        assert app.state.stream_slots._value == 7
        assert await anext(response.body_iterator) == wire
        pending = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await response.background()
        assert upstream.closed
        assert app.state.stream_slots._value == 8
        assert app.state.poll_slots._value == 8


@pytest.mark.parametrize('failure_at', ['start', 'body'])
async def test_stream_transport_failure_releases_even_unstarted_owner_once(repo, failure_at):
    wire = b'event: future\r\nid: custom\r\ndata: {"unknown":true}\r\n\r\n'
    upstream = Upstream(wire, content_type='text/event-stream; charset=utf-8', wait=True)
    async with gateway(repo, {(19811, '/events'): upstream}) as (app, _, _):
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/wrappers/{name}/{path:path}')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/wrappers/alpha/events',
                           'query_string': b'', 'headers': []})
        response = await endpoint('alpha', 'events', request)
        sent = []

        async def receive():
            await asyncio.Future()

        async def send(message):
            sent.append(message)
            if message['type'] == 'http.response.' + failure_at:
                raise OSError('isolated failed client transport')

        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(response(
                {'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send,
            ), 2)
        assert sent[0]['status'] == 200
        assert (b'content-type', b'text/event-stream; charset=utf-8') in sent[0]['headers']
        if failure_at == 'body':
            assert sent[1]['body'] == wire
        assert upstream.close_count == 1 and upstream.closed
        assert app.state.stream_slots._value == 8
        await response.aclose()
        await response.background()
        assert upstream.close_count == 1 and app.state.stream_slots._value == 8


@pytest.mark.parametrize('disconnect', ['asyncio_cancel', 'anyio_disconnect'])
async def test_idle_dashboard_stream_disconnect_closes_reader_and_owner_once(repo, disconnect):
    wire = b'event: future\ndata: {"unknown":true}\n\n'
    upstream = Upstream(wire, content_type='text/event-stream')
    reading, reader_closed, first_body = (asyncio.Event() for _ in range(3))

    class IdleContent:
        async def iter_chunked(self, size):
            try:
                yield wire
                reading.set()
                await asyncio.Future()
            finally:
                reader_closed.set()

    upstream.content = IdleContent()
    async with gateway(repo, {(19811, '/events'): upstream}) as (app, _, _):
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/wrappers/{name}/{path:path}')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/wrappers/alpha/events',
                           'query_string': b'', 'headers': []})
        response = await endpoint('alpha', 'events', request)

        async def receive():
            if disconnect == 'anyio_disconnect':
                await reading.wait()
                await first_body.wait()
                return {'type': 'http.disconnect'}
            await asyncio.Future()

        async def send(message):
            if message['type'] == 'http.response.body' and message.get('body'):
                assert message['body'] == wire
                first_body.set()

        task = asyncio.create_task(response(
            {'type': 'http', 'asgi': {'spec_version': '2.4' if disconnect == 'asyncio_cancel' else '2.0'}},
            receive, send,
        ))
        try:
            await asyncio.wait_for(asyncio.gather(reading.wait(), first_body.wait()), 2)
            if disconnect == 'asyncio_cancel':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                await asyncio.wait_for(task, 2)
            assert reader_closed.is_set() and not response._read_tasks
            assert upstream.close_count == 1 and app.state.stream_slots._value == 8
            await response.aclose()
            assert upstream.close_count == 1 and app.state.stream_slots._value == 8
        finally:
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            await response.aclose()


@pytest.mark.parametrize('upstream_error', [False, True])
async def test_dashboard_stream_eof_or_upstream_error_preserves_bytes_and_releases_once(repo, upstream_error):
    wire = b'event: future\ndata: {"unknown":true}\n\n'
    upstream = Upstream(wire, content_type='text/event-stream')

    class FiniteContent:
        async def iter_chunked(self, size):
            yield wire
            if upstream_error:
                raise RuntimeError('isolated upstream reader error')

    upstream.content = FiniteContent()
    async with gateway(repo, {(19811, '/events'): upstream}) as (app, _, _):
        endpoint = next(route.endpoint for route in app.routes
                        if getattr(route, 'path', '') == '/wrappers/{name}/{path:path}')
        request = Request({'type': 'http', 'method': 'GET', 'path': '/wrappers/alpha/events',
                           'query_string': b'', 'headers': []})
        response = await endpoint('alpha', 'events', request)
        sent = []

        async def receive():
            await asyncio.Future()

        async def send(message):
            sent.append(message)

        if upstream_error:
            with pytest.raises(RuntimeError, match='isolated upstream reader error'):
                await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        else:
            await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, receive, send)
        assert [message['body'] for message in sent if message.get('body')] == [wire]
        assert upstream.close_count == 1 and app.state.stream_slots._value == 8
        await response.aclose()
        assert upstream.close_count == 1 and app.state.stream_slots._value == 8


async def test_status_polling_progresses_with_full_stream_and_buffered_proxy_pools(repo):
    class CapacitySession(Session):
        """Model the real connector's shared connection reservations."""
        def __init__(self, responses, **kwargs):
            super().__init__(responses, **kwargs)
            self.connections = asyncio.Semaphore(self.connector.limit)
            self.buffered = 0
            self.buffers_full = asyncio.Event()

        async def get(self, url, **kwargs):
            await self.connections.acquire()
            try:
                response = await super().get(url, **kwargs)
            except BaseException:
                self.connections.release()
                raise
            original_close = response.close
            released = False

            def close():
                nonlocal released
                if not released:
                    released = True
                    original_close()
                    self.connections.release()

            response.close = close
            if urlsplit(url).path == "/metrics":
                self.buffered += 1
                if self.buffered == 8:
                    self.buffers_full.set()
            return response

    responses = {
        (19811, "/events"): lambda: Upstream(b"data: connected\n\n", content_type="text/event-stream", wait=True),
        (19811, "/metrics"): lambda: Upstream({"requests": 1}, wait=True),
    }
    for port in (19811, 19812):
        responses[(port, "/health")] = lambda: Upstream({"status": "ok"})
        responses[(port, "/ready")] = lambda: Upstream({"ready": True})
        responses[(port, "/stats")] = lambda: Upstream({"total_requests": 9})
    async with gateway(repo, responses, session_type=CapacitySession) as (app, client, session):
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/wrappers/{name}/{path:path}")
        request = Request({"type": "http", "method": "GET", "path": "/wrappers/alpha/events", "query_string": b"", "headers": []})
        streams = []
        buffers = []
        try:
            for _ in range(8):
                streams.append(await endpoint("alpha", "events", request))
            buffers = [asyncio.create_task(endpoint("alpha", "metrics", request)) for _ in range(8)]
            await asyncio.wait_for(session.buffers_full.wait(), timeout=1)
            assert app.state.stream_slots._value == 0
            assert app.state.slots._value == 0
            result = (await asyncio.wait_for(client.get("/api/status"), timeout=1)).json()
            assert result["counts"]["online"] == 2
            assert result["counts"]["ready"] == 2
            assert all(not task.done() for task in buffers)
        finally:
            for task in buffers:
                task.cancel()
            await asyncio.gather(*buffers, return_exceptions=True)
            for response in streams:
                await response.background()
        assert app.state.slots._value == 8
        assert app.state.stream_slots._value == 8
        assert app.state.poll_slots._value == 8
        assert session.connections._value == session.connector.limit


async def test_scope_changes_invalidate_global_cache_without_pending_io(repo):
    responses = {(19811, "/health"): lambda: Upstream({"status": "ok"})}
    async with gateway(repo, responses) as (_, client, session):
        await client.get("/api/status")
        session.calls.clear()
        path = repo / "wrappers.json"
        content = json.loads(path.read_text())
        content["wrappers"]["alpha"]["status"] = "pending"
        path.write_text(json.dumps(content))
        result = (await client.get("/api/status")).json()
        assert result["counts"]["active"] == 1
        assert all(urlsplit(url).port != 19811 for url, _ in session.calls)
        assert (await client.get("/wrappers/alpha/health")).status_code == 409


def test_numeric_projection_excludes_booleans_nonfinite_and_nested_secret_values():
    assert safe_numbers({"keys": True, "available": float("inf"), "total_requests": 9,
                         "metrics": {"total_tokens": 12, "secret": "bad"}, "key_pool": {"total_keys": 3, "available_keys": 2, "api_key": "bad"}}) == {
        "total_requests": 9, "total_tokens": 12, "total_keys": 3, "available_keys": 2,
    }


def test_missing_head_bridge_keeps_original_content():
    body = bridge_html("<body>same</body>", "alpha")
    assert body.endswith("<body>same</body>")


def test_browser_bridge_executes_prefixes_auth_headers_and_storage_isolation():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for browser bridge execution; browser smoke covers deployment")
    scripts = {name: re.search(r"<script[^>]*>(.*?)</script>", bridge_html("<head></head>", name), re.S).group(1)
               for name in ("alpha", "beta")}
    harness = r"""
const vm = require('node:vm'), assert = require('node:assert/strict');
const scripts = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const shared = new Map();
function context(name) {
  class Storage {
    getItem(key) { return shared.get(key) ?? null; }
    setItem(key, value) { shared.set(key, String(value)); }
    removeItem(key) { shared.delete(key); }
  }
  class XMLHttpRequest { open(method, url) { this.method=method; this.url=url; } }
  function EventSource(url, options) { this.url=url; this.options=options; }
  EventSource.CONNECTING=0;
  const calls=[];
  const window={location:{href:'http://dashboard/wrappers/'+name+'/dashboard',origin:'http://dashboard'},
    fetch:(input,init)=>{calls.push({input,init});return Promise.resolve({});},EventSource};
  const ctx={URL,Request,Storage,XMLHttpRequest,window};
  vm.runInNewContext(scripts[name],ctx);
  return {window,calls,Storage,XMLHttpRequest};
}
const alpha=context('alpha'), beta=context('beta');
alpha.window.fetch('/metrics?window=24h&token=secret&api_key=secret',{headers:{Authorization:'Bearer manual'}});
assert.equal(alpha.calls[0].input,'http://dashboard/wrappers/alpha/metrics?window=24h');
assert.equal(alpha.calls[0].init.headers.Authorization,'Bearer manual');
alpha.window.fetch(new Request('http://dashboard/v1/models',{headers:{'x-api-key':'manual-key'}}));
assert.equal(alpha.calls[1].input.url,'http://dashboard/wrappers/alpha/v1/models');
assert.equal(alpha.calls[1].input.headers.get('x-api-key'),'manual-key');
alpha.window.fetch('/wrappers/alpha/stats');
assert.equal(alpha.calls[2].input,'http://dashboard/wrappers/alpha/stats');
assert.equal(new alpha.window.EventSource('/events').url,'http://dashboard/wrappers/alpha/events');
assert.equal(alpha.window.EventSource.CONNECTING,0);
const xhr=new alpha.XMLHttpRequest();xhr.open('GET','/health');
assert.equal(xhr.url,'http://dashboard/wrappers/alpha/health');
const a=new alpha.Storage(), b=new beta.Storage();
a.setItem('wrapper-bearer','alpha-token');
assert.equal(a.getItem('wrapper-bearer'),'alpha-token');
assert.equal(b.getItem('wrapper-bearer'),null);
b.setItem('wrapper-bearer','beta-token');
assert.equal(a.getItem('wrapper-bearer'),'alpha-token');
assert.equal(b.getItem('wrapper-bearer'),'beta-token');
a.removeItem('wrapper-bearer');
assert.equal(a.getItem('wrapper-bearer'),null);
assert.equal(b.getItem('wrapper-bearer'),'beta-token');
"""
    result = subprocess.run([node, "-e", harness], input=json.dumps(scripts), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
