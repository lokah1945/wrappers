from pathlib import Path

from common.model import CapabilityProfile, LimitProfile, ModelProfile, ModelProfileStore, ProtocolProfile


def test_profile_store_persists_authoritative_profile(tmp_path: Path):
    db = tmp_path / "profiles.db"
    profile = ModelProfile(
        canonical_id="nvidia/provider/model-a",
        provider="nvidia",
        provider_model_id="provider/model-a",
        profile_revision="manual-1",
        capabilities=CapabilityProfile(tools=True, reasoning=True),
        limits=LimitProfile(max_output_tokens=16384),
        protocols=(ProtocolProfile("openai_chat", "openai_chat", "/v1/chat/completions", "adapter.v1"),),
    )
    ModelProfileStore(db).save(profile)
    loaded = ModelProfileStore(db).load("nvidia")
    assert len(loaded) == 1
    assert loaded[0].profile_revision == "manual-1"
    assert loaded[0].capabilities.reasoning is True
    assert loaded[0].limits.max_output_tokens == 16384
