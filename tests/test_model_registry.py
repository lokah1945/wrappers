from pathlib import Path

from common.model import AliasBinding, LocalModelRegistry


ROOT = Path(__file__).resolve().parents[1]


def test_local_registry_loads_provider_manifest_and_registers_catalog():
    registry = LocalModelRegistry("nvidia", ROOT / "model-registry")
    ids = registry.register_catalog([{"id": "provider/model-a"}], revision="r1")
    assert ids == ["nvidia/provider/model-a"]
    plan = registry.call_plan("provider/model-a", "openai_chat")
    assert plan.model.provider_model_id == "provider/model-a"
    assert plan.model_substitution_allowed is False
    assert plan.key_rotation_allowed is True


def test_local_registry_alias_is_explicit_and_scoped():
    registry = LocalModelRegistry("nvidia", ROOT / "model-registry")
    registry.bind_alias(AliasBinding("client", "c1", "sonnet", "nvidia/provider/model-a", "r2"))
    registry.register_catalog(["provider/model-a"], revision="r1")
    plan = registry.call_plan("sonnet", "openai_chat", [("client", "c1")])
    assert plan.model.is_alias is True
    assert plan.model.provider_model_id == "provider/model-a"
    assert plan.model.resolution_revision == "r2"


def test_unknown_explicit_model_is_pass_through_not_rejected():
    registry = LocalModelRegistry("nous", ROOT / "model-registry")
    plan = registry.call_plan("unknown/provider-model", "openai_chat")
    assert plan.model.provider_model_id == "unknown/provider-model"
    assert plan.model_substitution_allowed is False
