from pathlib import Path

from common.model import AliasBinding, LocalModelRegistry


def test_alias_binding_persists_with_profile_store(tmp_path: Path):
    db = tmp_path / "profiles.db"
    first = LocalModelRegistry("nvidia", Path("model-registry"), db)
    first.bind_alias(AliasBinding("client", "c1", "sonnet", "nvidia/provider/model-a", "r1"))
    second = LocalModelRegistry("nvidia", Path("model-registry"), db)
    resolved = second.resolve("sonnet", [("client", "c1")])
    assert resolved.is_alias is True
    assert resolved.provider_model_id == "provider/model-a"
