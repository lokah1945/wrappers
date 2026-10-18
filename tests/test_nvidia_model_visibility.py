from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python"))

from src.main import filter_available_models, requested_model_visibility  # noqa: E402
from starlette.requests import Request  # noqa: E402


def make_request(query: bytes = b"", dashboard: bool = False):
    headers = [(b"x-wrapper-dashboard", b"1")] if dashboard else []
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models",
        "query_string": query, "headers": headers, "scheme": "http",
        "server": ("test", 80), "client": ("test", 1),
    })


def test_default_visibility_is_available():
    assert requested_model_visibility(make_request()) == "available"
    rows = [
        {"id": "ok/model", "availability_state": "available", "available": True},
        {"id": "unknown/model", "availability_state": "network_timeout"},
        {"id": "dead/model", "availability_state": "wrong_route", "available": False},
    ]
    assert [row["id"] for row in filter_available_models(rows)] == ["ok/model"]


def test_public_clients_cannot_expand_the_model_list():
    assert requested_model_visibility(make_request(b"availability=all")) == "available"


def test_dashboard_all_visibility_is_explicit_and_keeps_diagnostics():
    assert requested_model_visibility(make_request(b"availability=all", dashboard=True)) == "all"
    rows = [
        {"id": "ok/model", "availability_state": "available"},
        {"id": "unknown/model", "availability_state": "network_timeout"},
    ]
    assert [row["id"] for row in filter_available_models(rows, "all")] == [
        "ok/model", "unknown/model"
    ]
