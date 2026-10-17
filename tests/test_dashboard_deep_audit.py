"""Deep control-dashboard contract checks for active-wrapper aggregation."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import pytest

from dashboard.app import create_app

pytestmark = pytest.mark.scope_policy


class Content:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, size):
        yield self.body


class Response:
    def __init__(self, payload, status=200, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = Content(raw)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses, **kwargs):
        self.responses = responses
        self.calls = []
        self.closed = False

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        # Yield once so concurrent requests exercise the shared refresh task.
        await asyncio.sleep(0)
        key = (urlsplit(url).port, urlsplit(url).path)
        value = self.responses.get(key, Response({}, status=404))
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    async def close(self):
        self.closed = True


@pytest.fixture
def repo(tmp_path):
    entries = {
        "alpha": {"status": "production", "port": 19811, "upstream": "Alpha", "supports": ["openai_chat"], "dashboard_stream": "native_sse"},
        "beta": {"status": "production", "port": 19812, "upstream": "Beta", "supports": ["openai_chat"], "dashboard_stream": "polling"},
        "blackbox": {"status": "pending", "port": 19813, "upstream": "Frozen one"},
        "opencode": {"status": "pending", "port": 19814, "upstream": "Frozen two"},
    }
    (tmp_path / "wrappers.json").write_text(json.dumps({"wrappers": entries}))
    return tmp_path


@asynccontextmanager
async def gateway(repo, responses):
    holder = {}

    def factory(**kwargs):
        holder["session"] = Session(responses, **kwargs)
        return holder["session"]

    app = create_app(repo, session_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard") as client:
            yield app, client, holder["session"]
    assert holder["session"].closed


def responses():
    out = {}
    for port in (19811, 19812):
        out[(port, "/health")] = Response({"status": "ok", "keys": 2, "available": 2, "total_requests": port})
        out[(port, "/ready")] = Response({"ready": True})
        out[(port, "/stats")] = Response({"total_requests": port, "total_tokens": 12})
        out[(port, "/v1/models")] = Response({"object": "list", "data": [{"id": "m-1"}, {"id": "m-2"}]})
        out[(port, "/v1/capabilities")] = Response({"object": "list", "models": [{"id": "m-1"}]})
    return out


@pytest.mark.asyncio
async def test_status_orders_active_ports_then_frozen_and_reports_endpoint_coverage(repo):
    async with gateway(repo, responses()) as (_, client, _):
        data = (await client.get("/api/status")).json()
    assert [item["port"] for item in data["wrappers"]] == [19811, 19812, 19813, 19814]
    assert [item["status"] for item in data["wrappers"]] == ["online", "online", "pending", "pending"]
    assert data["wrappers"][0]["models_count"] == 2
    assert data["wrappers"][0]["capabilities_count"] == 1
    assert data["wrappers"][0]["realtime"] == "native_sse"
    assert data["wrappers"][1]["realtime"] == "polling"
    assert data["wrappers"][0]["endpoint_status"] == {
        "health": 200, "ready": 200, "stats": 200, "metrics": None,
        "models": 200, "capabilities": 200, "usage": 404,
        "activity": 404, "chart_hourly": 404,
    }
    assert data["model_planes"]["model_fetcher"]["port"] == 8787
    assert data["model_planes"]["model_registry"]["port"] == 9200
    assert data["counts"]["inference_active"] == 2


@pytest.mark.asyncio
async def test_stats_fallback_preserves_missing_endpoint_visibility(repo):
    data_responses = responses()
    data_responses[(19811, "/stats")] = Response({}, status=404)
    data_responses[(19811, "/metrics")] = Response({"total_requests": 77, "errors": 2})
    async with gateway(repo, data_responses) as (_, client, _):
        service = next(item for item in (await client.get("/api/status")).json()["wrappers"] if item["name"] == "alpha")
    assert service["endpoint_status"]["stats"] == 404
    assert service["endpoint_status"]["metrics"] == 200
    assert service["counters"]["total_requests"] == 77


@pytest.mark.asyncio
async def test_status_refresh_is_single_flight_and_does_not_probe_pending(repo):
    async with gateway(repo, responses()) as (_, client, session):
        one, two = await asyncio.gather(client.get("/api/status"), client.get("/api/status"))
    assert one.status_code == two.status_code == 200
    wrapper_calls = [(url, kwargs) for url, kwargs in session.calls if urlsplit(url).port in (19811, 19812)]
    assert len(wrapper_calls) == 16  # eight telemetry probes per active wrapper, not doubled
    assert all(urlsplit(url).port in (19811, 19812) for url, _ in wrapper_calls)
    assert len(session.calls) < 2 * (16 + 16)  # model-plane probes also share the flight


@pytest.mark.asyncio
async def test_status_forwards_browser_auth_to_all_read_probes_without_exposing_it(repo):
    async with gateway(repo, responses()) as (_, client, session):
        response = await client.get("/api/status", headers={"Authorization": "Bearer dashboard-token"})
        assert response.status_code == 200
    assert session.calls
    for _, kwargs in session.calls:
        assert kwargs["headers"].get("authorization") == "Bearer dashboard-token"
    assert "dashboard-token" not in response.text


@pytest.mark.asyncio
async def test_status_sse_emits_retry_and_live_snapshot_then_closes_cleanly(repo):
    async with gateway(repo, responses()) as (app, _, _):
        route = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/status/stream")
        from starlette.requests import Request
        request = Request({"type": "http", "method": "GET", "path": "/api/status/stream", "query_string": b"", "headers": []})
        response = await route(request)
        first = await asyncio.wait_for(anext(response.body_iterator), 1)
        second = await asyncio.wait_for(anext(response.body_iterator), 3)
        assert first == b"retry: 3000\n\n"
        assert second.startswith(b"event: status\ndata: {")
        assert b'"port":19811' in second and b'"port":19812' in second
        await response.aclose()

@pytest.mark.asyncio
async def test_catalog_models_are_bounded_redacted_and_registry_rows_excluded(tmp_path):
    entries = {
        "nvidia-python": {"status": "production", "port": 19811, "upstream": "NVIDIA", "supports": ["openai_chat"]},
        "model-registry": {"status": "production", "port": 19200, "upstream": "internal", "service_kind": "internal"},
    }
    (tmp_path / "wrappers.json").write_text(json.dumps({"wrappers": entries}))
    catalog = {"models": [{"catalog_id": "nvidia/demo", "served_model_id": "demo", "title": "Demo", "secret": "PRIVATE_TOKEN", "description": "x" * 300000}]}
    data_responses = {
        (19811, "/health"): Response({"status": "ok"}),
        (19811, "/ready"): Response({"ready": True}),
        (19811, "/v1/models"): Response({"data": [{"id": "demo"}]}),
        (19811, "/v1/capabilities"): Response({"models": [{"id": "demo"}]}),
        (8787, "/health"): Response({"ok": True}),
        (8787, "/ready"): Response({"ready": True}),
        (8787, "/models"): Response(catalog),
        (19200, "/health"): Response({"status": "ok"}),
        (19200, "/ready"): Response({"ready": True}),
        (19200, "/stats"): Response({"models": 1}),
    }
    async with gateway(tmp_path, data_responses) as (_, client, _):
        response = await client.get("/api/status")
    assert response.status_code == 200
    assert "PRIVATE_TOKEN" not in response.text
    assert len(response.content) < 250_000
    data = response.json()
    assert data["model_planes"]["model_fetcher"]["models"][0]["catalog_id"] == "nvidia/demo"
    assert data["model_planes"]["model_registry"]["providers"] == {}
    assert data["model_planes"]["model_registry"]["model_feed"] == "excluded"


@pytest.mark.asyncio
async def test_model_plane_failure_is_isolated_from_wrapper_status(tmp_path):
    entries = {
        "nvidia-python": {"status": "production", "port": 19811, "upstream": "NVIDIA", "supports": ["openai_chat"]},
    }
    (tmp_path / "wrappers.json").write_text(json.dumps({"wrappers": entries}))
    responses_map = {
        (19811, "/health"): Response({"status": "ok"}),
        (19811, "/ready"): Response({"ready": True}),
    }
    async with gateway(tmp_path, responses_map) as (_, client, _):
        response = await client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert next(item for item in data["wrappers"] if item["name"] == "nvidia-python")["status"] == "online"
    assert data["model_planes"]["model_fetcher"]["status"] in ("offline", "degraded")
    assert data["model_planes"]["model_registry"]["status"] in ("offline", "degraded")
