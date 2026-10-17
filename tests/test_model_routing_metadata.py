from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python" / "src"))

from common.model.routing import (  # noqa: E402
    enrich_openai_catalog_entry,
    endpoint_descriptor,
    join_upstream_url,
)
from capabilities import describe  # noqa: E402


def test_join_does_not_duplicate_openrouter_v1():
    assert join_upstream_url(
        "https://openrouter.ai/api/v1", "/v1/chat/completions"
    ) == "https://openrouter.ai/api/v1/chat/completions"


def test_join_keeps_v1_for_host_only_provider():
    assert join_upstream_url(
        "https://inference-api.nousresearch.com", "/v1/chat/completions"
    ) == "https://inference-api.nousresearch.com/v1/chat/completions"


def test_catalog_entry_has_exact_wrapper_route_and_preserves_provider_metadata():
    source = {"id": "demo/model", "architecture": {"modality": "text->text"}}
    enriched = enrich_openai_catalog_entry(
        source, "openrouter", "https://openrouter.ai/api/v1"
    )
    assert enriched["id"] == source["id"]
    assert enriched["endpoints"][0]["url"] == (
        "https://openrouter.ai/api/v1/chat/completions"
    )
    assert enriched["endpoints"][0]["route_confidence"] == "exact"
    assert enriched["routing_metadata"]["model_identity_preserved"] is True
    assert source.get("endpoints") is None


def test_nvidia_chat_descriptor_exposes_resolved_url():
    endpoint = describe("nvidia/nemotron-3-super-120b-a12b")["endpoints"][0]
    assert endpoint["client_path"] == "/v1/chat/completions"
    assert endpoint["url"] == (
        "https://integrate.api.nvidia.com/v1/chat/completions"
    )
    assert endpoint["route_confidence"] == "exact"


def test_nvidia_image_descriptor_exposes_model_url_template():
    endpoint = describe("black-forest-labs/flux.1-schnell")["endpoints"][0]
    assert endpoint["client_path"] == "/v1/images/generations"
    assert endpoint["url_template"] == (
        "https://ai.api.nvidia.com/v1/genai/{model}"
    )
    assert endpoint["upstream_path_template"] == "/v1/genai/{model}"


def test_synthetic_video_detector_exposes_native_grpc_contract():
    descriptor = describe("nvidia/ai-synthetic-video-detector")
    assert descriptor["type"] == "video_analysis"
    assert "grpc" in descriptor["capabilities"]
    endpoint = descriptor["endpoints"][0]
    assert endpoint["protocol"] == "grpc"
    assert endpoint["url"] == "grpc://grpc.nvcf.nvidia.com:443"
    assert endpoint["client_path"] is None
    assert endpoint["client"] == "NVIDIA Maxine generated protobuf client"
