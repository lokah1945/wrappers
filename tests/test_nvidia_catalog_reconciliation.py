from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "nvidia-python"))
sys.path.insert(0, str(ROOT / "nvidia-python" / "src"))

from capabilities import CURATED_GENAI, classify, describe  # noqa: E402
from src.main import _probe_spec  # noqa: E402


def test_flux_3_is_explicitly_curated_on_native_genai_surface():
    assert "black-forest-labs/flux-3-image-klein" in CURATED_GENAI
    descriptor = describe("black-forest-labs/flux-3-image-klein")
    assert descriptor["type"] == "image"
    endpoint = descriptor["endpoints"][0]
    assert endpoint["client_path"] == "/v1/images/generations"
    assert endpoint["url_template"] == "https://ai.api.nvidia.com/v1/genai/{model}"
    assert descriptor["catalog_metadata"]["build_access"] == "downloadable"
    assert descriptor["catalog_metadata"]["provider_catalog_presence"] == "not_listed_at_reconciliation"


def test_flux_3_probe_does_not_use_generic_chat_or_downloadable_assumption():
    spec = _probe_spec("black-forest-labs/flux-3-image-klein")
    assert spec["endpoint"] == "/v1/genai/black-forest-labs/flux.3-image-klein"
    assert spec["body"] == {
        "prompt": "health check",
        "width": 1024,
        "height": 1024,
        "samples": 1,
        "seed": 0,
        "steps": 16,
    }


def test_llama_guard_is_multimodal_and_uses_provider_chat_contract():
    descriptor = classify("meta/llama-guard-4-12b")
    assert descriptor["type"] == "vision_chat"
    spec = _probe_spec("meta/llama-guard-4-12b")
    assert spec["endpoint"] == "/v1/chat/completions"
    body = spec["body"]
    assert body["model"] == "meta/llama-guard-4-12b"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["max_tokens"] == 5
    assert body["stream"] is False
    assert "conversation" not in body
