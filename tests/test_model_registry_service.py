import importlib.util
import os
import threading
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_service(tmp_path):
    os.environ["MODEL_REGISTRY_DB"] = str(tmp_path / "registry.db")
    os.environ["MODEL_REGISTRY_ADMIN_TOKEN"] = "test-token"
    spec = importlib.util.spec_from_file_location(
        "model_registry_service_test", ROOT / "model-registry" / "service.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_registry_service_resolves_exact_model_without_fallback(tmp_path):
    service = load_service(tmp_path)
    client = TestClient(service.app)
    response = client.post(
        "/v1/resolve",
        json={"provider": "nvidia", "requested_model": "provider/model-a"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resolved"]["provider_model_id"] == "provider/model-a"
    assert body["model_substitution"] is False


def test_registry_service_ingests_scoped_alias(tmp_path):
    service = load_service(tmp_path)
    client = TestClient(service.app)
    response = client.post(
        "/internal/aliases",
        json={
            "provider": "nvidia",
            "bindings": [{
                "scope_type": "client",
                "scope_id": "claude-a",
                "alias": "sonnet",
                "canonical_target": "nvidia/provider/model-a",
                "revision": "r1",
            }],
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    resolved = client.post(
        "/v1/resolve",
        json={
            "provider": "nvidia",
            "requested_model": "sonnet",
            "scope_chain": [["client", "claude-a"]],
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"]["provider_model_id"] == "provider/model-a"


def test_registry_service_ingests_catalog_and_returns_call_plan(tmp_path):
    service = load_service(tmp_path)
    client = TestClient(service.app)
    ingest = client.post(
        "/internal/catalog",
        json={"provider": "nvidia", "revision": "r1", "models": [{"id": "provider/model-a"}]},
        headers={"Authorization": "Bearer test-token"},
    )
    assert ingest.status_code == 200
    plan = client.post(
        "/v1/call-plan",
        json={
            "provider": "nvidia",
            "requested_model": "provider/model-a",
            "client_surface": "openai_chat",
        },
    )
    assert plan.status_code == 200
    body = plan.json()["plan"]
    assert body["model"]["provider_model_id"] == "provider/model-a"
    assert body["model_substitution_allowed"] is False


def test_registry_service_health_lists_ingested_provider(tmp_path):
    """R9: /health provider snapshot works and reflects ingested providers."""
    service = load_service(tmp_path)
    client = TestClient(service.app)
    ingest = client.post(
        "/internal/catalog",
        json={"provider": "nvidia", "revision": "r1", "models": [{"id": "provider/model-a"}]},
        headers={"Authorization": "Bearer test-token"},
    )
    assert ingest.status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert "nvidia" in health.json()["providers_loaded"]


def test_registry_service_health_provider_snapshot_race_free(tmp_path):
    """R9 (MR-2 class): /health previously iterated `central.registries`
    directly while catalog ingest (threadpool) could create a provider —
    RuntimeError "dictionary changed size during iteration" → 500 on the
    monitoring endpoint. The guarded providers() snapshot is race-free."""
    service = load_service(tmp_path)
    errors = []

    def create_providers():
        try:
            for i in range(8):
                service.central.registry(f"provider-{i}")
        except Exception as exc:  # pragma: no cover - test failure path
            errors.append(exc)

    t = threading.Thread(target=create_providers)
    t.start()
    for _ in range(400):
        snap = service.central.providers()
        assert isinstance(snap, list)
    t.join()
    assert not errors, f"provider creation failed: {errors}"
    assert len(service.central.providers()) == 8


def test_registry_service_alias_ingest_off_loop_guarded(tmp_path):
    """R9 (MR-2 class): source lock — alias ingest must not run its sync
    SQLite binding loop on the event loop / outside the guard."""
    src = (ROOT / "model-registry" / "service.py").read_text()
    # the binding loop now lives behind CentralRegistry.bind_aliases, which
    # takes the guard, and is invoked via asyncio.to_thread from the handler
    assert "to_thread(central.bind_aliases" in src
    bind_aliases_body = src.split("def bind_aliases", 1)[1].split("\n    def ", 1)[0]
    assert "with self._guard" in bind_aliases_body
    # /health no longer iterates the registries dict directly (docstrings
    # quoting the old call are fine — look for the actual expression site)
    health_body = src.split("def health()", 1)[1].split("def ", 1)[0]
    assert "sorted(central.registries)" not in health_body
    assert "central.providers()" in src


def test_registry_service_alias_ingest_still_works(tmp_path):
    """R9: moving alias ingest into a guarded thread must not change the
    client-visible behaviour (still registers + resolves)."""
    service = load_service(tmp_path)
    client = TestClient(service.app)
    response = client.post(
        "/internal/aliases",
        json={
            "provider": "blackbox",
            "bindings": [{
                "scope_type": "client",
                "scope_id": "claude-b",
                "alias": "opus",
                "canonical_target": "blackbox/provider/model-x",
                "revision": "r2",
            }],
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    assert response.json()["registered"] == 1
    resolved = client.post(
        "/v1/resolve",
        json={
            "provider": "blackbox",
            "requested_model": "opus",
            "scope_chain": [["client", "claude-b"]],
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["resolved"]["provider_model_id"] == "provider/model-x"
