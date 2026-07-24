from pathlib import Path

from common.model import CapabilityProfile, LimitProfile, LocalModelRegistry, ModelProfile, ProtocolProfile


ROOT = Path(__file__).resolve().parents[1]


def test_catalog_refresh_preserves_authoritative_profile(tmp_path):
    registry = LocalModelRegistry("nvidia", ROOT / "model-registry")
    profile = ModelProfile(
        canonical_id="nvidia/provider/model-a",
        provider="nvidia",
        provider_model_id="provider/model-a",
        profile_revision="manual-r7",
        capabilities=CapabilityProfile(tools=True, reasoning=True),
        limits=LimitProfile(max_output_tokens=16384),
        protocols=(ProtocolProfile("openai_chat", "openai_chat", "/v1/chat/completions", "custom.v3"),),
        request_rules={"remove_parameters": ["reasoning_effort"]},
        provenance={"source": "manual_manifest"},
    )
    registry.register_profile(profile)
    registry.register_catalog([{"id": "provider/model-a", "metadata": "new-catalog"}], revision="catalog-r8")
    result = registry.profiles["nvidia/provider/model-a"]
    assert result.profile_revision == "manual-r7"
    assert result.limits.max_output_tokens == 16384
    assert result.capabilities.reasoning is True
    assert result.protocols[0].adapter_name == "custom.v3"
    assert result.request_rules["remove_parameters"] == ["reasoning_effort"]
    assert result.catalog_revision == "catalog-r8"
