"""Registry malformed requests never become 500s or partially change aliases."""

import asyncio
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_model_registry_service import load_service


@pytest.mark.parametrize("path", ["/v1/resolve", "/v1/call-plan"])
@pytest.mark.parametrize("scope", ["wrapper", 7, {}, ["wrong"], [["client"]], [["client", []]]])
def test_malformed_scopes_return_400(tmp_path, path, scope):
    service = load_service(tmp_path)
    response = TestClient(service.app, raise_server_exceptions=False).post(path, json={
        "provider": "nvidia", "requested_model": "vendor/model", "client_surface": "openai_chat",
        "scope_chain": scope,
    })
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "registry_error"


@pytest.mark.parametrize("model", [["model"], {"model": "m"}, "vendor/../model", "model%0Aevil"])
def test_invalid_requested_model_returns_400(tmp_path, model):
    service = load_service(tmp_path)
    response = TestClient(service.app).post("/v1/resolve", json={"provider": "nvidia", "model": model})
    assert response.status_code == 400


@pytest.mark.parametrize("status", ["invalid", [], {}, True, 200.5, -1, 600])
def test_invalid_http_status_returns_400_without_writing(tmp_path, status):
    service = load_service(tmp_path)
    client = TestClient(service.app, raise_server_exceptions=False)
    response = client.post("/internal/observations", headers={"Authorization": "Bearer test-token"}, json={
        "provider": "nvidia", "canonical_model_id": "nvidia/vendor/model", "state": "available",
        "http_status": status,
    })
    assert response.status_code == 400
    assert service.central.state("nvidia").status_map() == {}


def test_alias_validation_rejects_whole_batch_without_partial_write(tmp_path):
    service = load_service(tmp_path)
    client = TestClient(service.app)
    response = client.post("/internal/aliases", headers={"Authorization": "Bearer test-token"}, json={
        "provider": "nvidia", "bindings": [
            {"alias": "sonnet", "canonical_target": "nvidia/vendor/model"},
            {"alias": "invalid", "canonical_target": "blackbox/vendor/model"},
        ],
    })
    assert response.status_code == 400
    registry = service.central.registry("nvidia")
    assert registry.resolve("sonnet").is_alias is False
    assert registry.profile_store.load_aliases("nvidia") == []


@pytest.mark.parametrize("path,key", [("/internal/catalog", "models"), ("/internal/aliases", "bindings")])
def test_falsey_nonlist_batches_are_rejected(tmp_path, path, key):
    service = load_service(tmp_path)
    response = TestClient(service.app).post(path, headers={"Authorization": "Bearer test-token"}, json={
        "provider": "nvidia", key: {},
    })
    assert response.status_code == 400


async def test_cold_registry_load_does_not_block_event_loop(tmp_path, monkeypatch):
    service = load_service(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = service.central.registry

    def slow_registry(provider):
        started.set()
        assert release.wait(timeout=2)
        return original(provider)

    monkeypatch.setattr(service.central, "registry", slow_registry)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(service.app), base_url="http://registry.test") as client:
        pending = asyncio.create_task(client.post("/v1/resolve", json={"provider": "nvidia", "model": "vendor/model"}))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            # This timer cannot execute until the simulated SQLite load releases
            # if the endpoint performs that load directly on the event loop.
            await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
        finally:
            release.set()
        response = await pending
    assert response.status_code == 200


def test_registry_applies_shared_body_cap(tmp_path):
    service = load_service(tmp_path)
    response = TestClient(service.app).post(
        "/v1/resolve", content=b"{}", headers={"Content-Length": str(11 * 1024 * 1024)},
    )
    assert response.status_code == 413
