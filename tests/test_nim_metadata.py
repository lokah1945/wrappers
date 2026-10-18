from __future__ import annotations

import asyncio
import json

from common.nim_metadata import (
    NimMetadataRegistry,
    choose_tag,
    normalize_profile,
    profile_fit,
    public_metadata,
    resource_candidates,
)


class _Response:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        for prefix, response in self.responses:
            if url.startswith(prefix):
                return _Response(*response)
        return _Response(404, {})


def test_profile_normalization_derives_gpu_requirement_and_preserves_unknown_fields():
    result = normalize_profile({
        "profile_id": "a" * 64,
        "gpu_device": "2330:10de",
        "tensor_parallel_size": 2,
        "pipeline_parallel_size": 3,
        "min_vram_per_device_gb": "80",
        "future_field": {"enabled": True},
    })
    assert result["required_gpus"] == 6
    assert result["min_vram_per_device_gb"] == 80
    assert result["future_field"] == {"enabled": True}


def test_resource_candidates_are_conservative_and_accept_verified_aliases():
    row = {"id": "nvidia/nemotron-parse-2.0", "title": "nemotron-parse-2.0", "downloadable": True}
    candidates = resource_candidates(row)
    assert candidates[-1] == "nim/nvidia/nemotron-parse"
    assert resource_candidates({"id": "nvidia/foo", "downloadable": False}) == []


def test_choose_tag_prefers_explicit_then_repository_latest_then_highest_stable():
    repo = {"latestTag": "latest", "tags": ["1.2.0", "1.10.0", "latest", "1.10.0-variant"]}
    assert choose_tag(repo) == "1.10.0"
    assert choose_tag(repo, "1.2.0") == "1.2.0"


def test_profile_fit_never_claims_fit_without_hardware():
    profile = {"required_gpus": 2, "min_vram_per_device_gb": 80, "gpu_device": "2330:10de"}
    assert profile_fit(profile) == "unknown"
    assert profile_fit(profile, {"gpu_count": 1, "vram_per_device_gb": 80, "gpu_device": "2330:10de"}) == "does_not_fit"
    assert profile_fit(profile, {"gpu_count": 2, "vram_per_device_gb": 80, "gpu_device": "2330:10de"}) == "fit"


def test_registry_joins_repository_exact_tag_and_metadata_without_availability_claim(tmp_path):
    base = "https://api.test/v2"
    repo_url = f"{base}/repos/nim/nvidia/test-model"
    metadata_url = f"{base}/nim/metadata"
    session = _Session([
        (repo_url, (200, {"latestTag": "1.0", "tags": ["1.0", "latest"], "isPublic": True})),
        (metadata_url, (200, {"profiles": [{"profile_id": "p", "tensor_parallel_size": 2, "pipeline_parallel_size": 1}]})),
    ])
    rows = [{"id": "nvidia/test-model", "title": "test-model", "downloadable": True}]
    registry = NimMetadataRegistry(session=session, base_url=base, cache_file=tmp_path / "cache.json", ttl_sec=300)
    result = asyncio.run(registry.refresh(rows, force=True))
    entry = result["nvidia/test-model"]
    assert entry["metadata_status"] == "available"
    assert entry["resource_id"] == "nim/nvidia/test-model"
    assert entry["container_tag"] == "1.0"
    assert entry["profiles"][0]["required_gpus"] == 2
    assert "available" not in entry
    assert json.loads((tmp_path / "cache.json").read_text())["entries"]["nvidia/test-model"]["resource_id"] == "nim/nvidia/test-model"
    assert any("resourceId=nim%2Fnvidia%2Ftest-model" in url for url in session.urls)


def test_registry_distinguishes_missing_metadata_from_missing_repository(tmp_path):
    base = "https://api.test/v2"
    repo_url = f"{base}/repos/nim/nvidia/test-model"
    session = _Session([(repo_url, (200, {"latestTag": "1.0", "tags": ["1.0"]}))])
    row = {"id": "nvidia/test-model", "title": "test-model", "downloadable": True}
    registry = NimMetadataRegistry(session=session, base_url=base, cache_file=tmp_path / "cache.json", ttl_sec=300)
    result = asyncio.run(registry.refresh([row], force=True))
    assert result["nvidia/test-model"]["metadata_status"] == "metadata_not_published"

    missing = _Session([])
    registry = NimMetadataRegistry(session=missing, base_url=base, cache_file=tmp_path / "cache-2.json", ttl_sec=300)
    result = asyncio.run(registry.refresh([row], force=True))
    assert result["nvidia/test-model"]["metadata_status"] == "repository_not_found"


def test_public_metadata_can_include_full_profiles_or_descriptor_summaries():
    entry = {
        "model_id": "nvidia/test-model",
        "metadata_status": "available",
        "profiles": [{"profile_id": "p", "gpu": "H100", "required_gpus": 1}],
    }
    summary = public_metadata(entry)
    full = public_metadata(entry, include_profiles=True)
    assert "profiles" not in summary
    assert summary["profile_summaries"][0]["profile_id"] == "p"
    assert full["profiles"][0]["profile_id"] == "p"
