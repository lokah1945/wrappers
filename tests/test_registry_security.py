import importlib.util
import os
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def load_service(tmp_path, token=""):
    os.environ["MODEL_REGISTRY_DB"] = str(tmp_path / "registry.db")
    os.environ["MODEL_REGISTRY_ADMIN_TOKEN"] = token
    spec = importlib.util.spec_from_file_location(
        f"registry_security_{abs(hash(str(tmp_path)))}", ROOT / "model-registry" / "service.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_internal_write_fails_closed_without_admin_token(tmp_path):
    service = load_service(tmp_path, token="")
    response = TestClient(service.app).post(
        "/internal/catalog",
        json={"provider": "nvidia", "models": [{"id": "provider/model-a"}]},
    )
    assert response.status_code == 503


def test_internal_write_rejects_wrong_admin_token(tmp_path):
    service = load_service(tmp_path, token="expected")
    response = TestClient(service.app).post(
        "/internal/catalog",
        json={"provider": "nvidia", "models": [{"id": "provider/model-a"}]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401
