import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python"))
sys.path.insert(0, str(ROOT / "nvidia-python" / "src"))

from common.nvidia_build_catalog import (  # noqa: E402
    BUILD_MODEL_IDS,
    apply_build_website_catalog,
    get_build_contract,
)
import asyncio

import common.nvidia_build_web_catalog as web_catalog  # noqa: E402
from common.nvidia_build_web_catalog import (  # noqa: E402
    NvidiaBuildWebCatalogRegistry,
    parse_build_models_page,
)
from src.capabilities import build_catalog  # noqa: E402
from src.main import build_access_metadata  # noqa: E402


def _resource(publisher, name, nim_types, *, usecase=None, provider=None):
    labels = [
        {"key": "publisher", "values": [publisher]},
        {"key": "nimType", "values": nim_types},
    ]
    if usecase:
        labels.append({"key": "usecase", "values": usecase})
    if provider:
        labels.append({"key": "cloudPartnerType", "values": provider})
    return {
        "resourceType": "ENDPOINT",
        "resourceId": f"org/{name}",
        "name": name,
        "displayName": name,
        "description": "description",
        "labels": labels,
        "attributes": [{"key": "AVAILABLE", "value": "true"}],
    }


def _html(resources, total):
    group = {"groupValue": "_scored", "totalCount": len(resources), "resources": resources}
    payload = f"\"resultPageTotal\":1,\"resultTotal\":{total},\"results\":" + json.dumps([group], separators=(",", ":"))[1:-1]
    # The parser looks for a JSON group object in a decoded RSC segment.
    return "<script>self.__next_f.push(" + json.dumps([1, payload]) + ")</script>"


def test_parse_live_result_total_and_exact_nimtype_facets():
    html = _html([
        _resource("nvidia", "free", ["Free Endpoint"], usecase=["Text-to-Text"]),
        _resource("nvidia", "both", ["Partner Endpoint", "Download Available"]),
    ], 2)
    parsed = parse_build_models_page(html)
    assert parsed["result_total"] == 2
    assert parsed["page_total"] == 1
    assert [row["id"] for row in parsed["models"]] == ["nvidia/free", "nvidia/both"]
    assert parsed["models"][0]["free_endpoint"] is True
    assert parsed["models"][1]["partner_endpoint"] is True
    assert parsed["models"][1]["download_available"] is True
    assert parsed["models"][0]["use_cases"] == ["Text-to-Text"]


def test_website_overlay_replaces_stale_flags_and_preserves_contract():
    original_count = len(BUILD_MODEL_IDS)
    body_contract = get_build_contract("nvidia/body-pose")
    rows = [{
        "id": "nvidia/body-pose",
        "title": "3D Body Pose",
        "build_url": "https://build.nvidia.com/nvidia/body-pose",
        "website_listed": True,
        "website_source": "https://build.nvidia.com/models",
        "website_access_modes": ["free_endpoint", "partner_endpoint", "download_available"],
        "website_badges": ["Free Endpoint", "Partner Endpoint", "Download Available"],
        "access_badges": ["Free Endpoint", "Partner Endpoint", "Download Available"],
        "free_endpoint": True,
        "partner_endpoint": True,
        "download_available": True,
        "downloadable": True,
        "publisher": "nvidia",
        "use_cases": [],
        "inference_providers": [],
        "nim_container_gpus": [],
    }]
    try:
        apply_build_website_catalog(rows)
        current = get_build_contract("nvidia/body-pose")
        assert len(BUILD_MODEL_IDS) == original_count
        assert current["contract"] == body_contract["contract"]
        assert current["partner_endpoint"] is True
        metadata = build_access_metadata("nvidia/body-pose")
        assert metadata["partner_endpoint"] is True
        assert "partner_endpoint" in metadata["access_modes"]
        assert "Download Available" in metadata["access_badges"]
    finally:
        apply_build_website_catalog([])


def test_build_surface_is_lower_bound_and_backend_rows_are_additive():
    website_rows = build_catalog([], "https://integrate.api.nvidia.com", "https://ai.api.nvidia.com")
    assert len(website_rows) >= 99
    with_backend = build_catalog(
        ["backend/verified-model"],
        "https://integrate.api.nvidia.com",
        "https://ai.api.nvidia.com",
    )
    assert len(with_backend) == len(website_rows) + 1
    assert any(row["id"] == "backend/verified-model" for row in with_backend)


def test_registry_keeps_last_good_snapshot_when_refresh_fails(tmp_path, monkeypatch):
    good_row = {
        "id": "nvidia/good",
        "nim_type": ["Free Endpoint"],
        "free_endpoint": True,
        "partner_endpoint": False,
        "download_available": False,
    }

    async def good_fetch(*args, **kwargs):
        return {
            "schema": 1,
            "source": "https://build.nvidia.com/models",
            "retrieved_at": 123.0,
            "result_total": 1,
            "page_total": 1,
            "models": [good_row],
        }

    async def bad_fetch(*args, **kwargs):
        raise RuntimeError("website timeout")

    async def run():
        registry = NvidiaBuildWebCatalogRegistry(
            None, cache_file=tmp_path / "build-cache.json", enabled=True
        )
        monkeypatch.setattr(web_catalog, "fetch_build_web_catalog", good_fetch)
        assert (await registry.refresh(force=True))[0]["id"] == "nvidia/good"
        assert (tmp_path / "build-cache.json").exists()
        monkeypatch.setattr(web_catalog, "fetch_build_web_catalog", bad_fetch)
        assert (await registry.refresh(force=True))[0]["id"] == "nvidia/good"
        assert "RuntimeError" in registry.status()["last_error"]

    asyncio.run(run())
