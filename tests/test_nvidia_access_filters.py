from pathlib import Path
import sys

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python"))
sys.path.insert(0, str(ROOT / "nvidia-python" / "src"))

from src.main import (  # noqa: E402
    build_access_metadata,
    filter_access_models,
    requested_access_filter,
)


def make_request(query: bytes = b""):
    return Request({
        "type": "http", "method": "GET", "path": "/v1/models",
        "query_string": query, "headers": [], "scheme": "http",
        "server": ("test", 80), "client": ("test", 1),
    })


def test_access_filter_aliases_match_build_website_categories():
    assert requested_access_filter(make_request(b"access=free_endpoint")) == "free_endpoint"
    assert requested_access_filter(make_request(b"mode=downloadable")) == "downloadable"
    assert requested_access_filter(make_request(b"access=download_available")) == "downloadable"
    assert requested_access_filter(make_request(b"access=partner_endpoint")) == "partner_endpoint"
    assert requested_access_filter(make_request(b"filter=free-endpoint")) == "free_endpoint"
    assert requested_access_filter(make_request()) is None


def test_access_filter_does_not_require_runtime_available_state():
    rows = [
        {"id": "free/model", "access_modes": ["free_endpoint"], "availability_state": "wrong_route"},
        {"id": "nim/model", "access_modes": ["downloadable"], "availability_state": "deployment_required"},
        {"id": "hosted/model", "access_modes": ["hosted_endpoint"], "availability_state": "available"},
        {"id": "partner/model", "access_modes": ["partner_endpoint"], "availability_state": "unknown"},
    ]
    assert [row["id"] for row in filter_access_models(rows, "free_endpoint")] == ["free/model"]
    assert [row["id"] for row in filter_access_models(rows, "downloadable")] == ["nim/model"]
    assert [row["id"] for row in filter_access_models(rows, "partner_endpoint")] == ["partner/model"]
    assert len(filter_access_models(rows, "all")) == 4


def test_build_metadata_preserves_both_free_and_downloadable_badges():
    body_pose = build_access_metadata("nvidia/body-pose")
    assert body_pose["free_endpoint"] is True
    assert body_pose["downloadable"] is True
    assert "free_endpoint" in body_pose["access_modes"]
    assert "downloadable" in body_pose["access_modes"]
    assert body_pose["catalog_usable"] is True

    qwen = build_access_metadata("qwen/qwen-image")
    assert qwen["downloadable"] is True
    assert qwen["free_endpoint"] is False
    assert qwen["usability_state"] == "downloadable_requires_deployment"
