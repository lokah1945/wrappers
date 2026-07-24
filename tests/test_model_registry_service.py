import importlib.util
import os
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
