from pathlib import Path
import sys

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python"))

from src.main import filter_available_models, requested_model_visibility  # noqa: E402


def make_request(query: bytes = b"", dashboard: bool = False):
    headers = [(b"x-wrapper-dashboard", b"1")] if dashboard else []
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models",
        "query_string": query, "headers": headers, "scheme": "http",
        "server": ("test", 80), "client": ("test", 1),
    })


def test_default_visibility_is_catalog_union():
    assert requested_model_visibility(make_request()) == "catalog"
    rows = [
        {"id": "ok/model", "availability_state": "available", "available": True},
        {"id": "unknown/model", "availability_state": "network_timeout"},
        {"id": "dead/model", "availability_state": "wrong_route", "available": False},
    ]
    # The runtime-only helper remains explicit and does not fabricate health.
    assert [row["id"] for row in filter_available_models(rows, "available")] == ["ok/model"]


def test_runtime_only_view_is_explicit():
    assert requested_model_visibility(make_request(b"availability=available")) == "available"


def test_public_clients_can_request_catalog_view():
    assert requested_model_visibility(make_request(b"availability=all")) == "catalog"


def test_dashboard_catalog_view_keeps_diagnostics():
    assert requested_model_visibility(make_request(b"availability=all", dashboard=True)) == "catalog"
    rows = [
        {"id": "ok/model", "availability_state": "available"},
        {"id": "unknown/model", "availability_state": "network_timeout"},
    ]
    assert [row["id"] for row in filter_available_models(rows, "catalog")] == [
        "ok/model", "unknown/model"
    ]
