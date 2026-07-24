import pytest

from common.model import (
    AliasBinding,
    AliasResolver,
    CapabilityProfile,
    CallPlanError,
    ErrorState,
    LimitProfile,
    ModelProfile,
    ProtocolProfile,
    build_call_plan,
    classify_upstream_error,
)


def profile() -> ModelProfile:
    return ModelProfile(
        canonical_id="nvidia/provider/model-a",
        provider="nvidia",
        provider_model_id="provider/model-a",
        capabilities=CapabilityProfile(streaming=True, tools=True),
        limits=LimitProfile(max_output_tokens=8192),
        protocols=(
            ProtocolProfile(
                client_surface="openai_chat",
                upstream_surface="openai_chat",
                path="/v1/chat/completions",
                adapter_name="nvidia.chat.v1",
            ),
        ),
    )


def test_concrete_identity_is_exact_and_catalog_independent():
    resolver = AliasResolver()
    ref = resolver.resolve("provider/model-a", "nvidia")
    assert ref.canonical_id == "nvidia/provider/model-a"
    assert ref.provider_model_id == "provider/model-a"
    assert ref.is_alias is False


def test_alias_is_scoped_and_does_not_use_last_model():
    resolver = AliasResolver()
    resolver.bind(AliasBinding("client", "claude-a", "sonnet", "nvidia/provider/model-a", "r1"))
    a = resolver.resolve("sonnet", "nvidia", [("client", "claude-a")])
    b = resolver.resolve("sonnet", "nvidia", [("client", "claude-b")])
    assert a.is_alias is True
    assert a.provider_model_id == "provider/model-a"
    assert b.is_alias is False
    assert b.provider_model_id == "sonnet"


def test_call_plan_contains_one_model_and_forbids_substitution():
    ref = AliasResolver().resolve("provider/model-a", "nvidia")
    plan = build_call_plan(profile(), ref, "openai_chat")
    assert plan.model.provider_model_id == "provider/model-a"
    assert plan.model_substitution_allowed is False
    assert plan.key_rotation_allowed is True


def test_invalid_profile_cannot_enable_substitution():
    bad = profile()
    bad = ModelProfile(**{**bad.__dict__, "policy": {"model_substitution": True}})
    ref = AliasResolver().resolve("provider/model-a", "nvidia")
    with pytest.raises(CallPlanError):
        build_call_plan(bad, ref, "openai_chat")


def test_error_classifier_only_rotates_key_and_never_changes_model():
    account = classify_upstream_error(404, {"detail": "Function x not found for account a"})
    key_limit = classify_upstream_error(429, {"error": "key rate limit"})
    eol = classify_upstream_error(410, "model reached its end of life")
    assert account.state == ErrorState.ACCOUNT_UNAVAILABLE
    assert account.rotate_key is True
    assert account.hard_block is False
    assert key_limit.rotate_key is True
    assert eol.state == ErrorState.GLOBALLY_RETIRED
    assert eol.hard_block is True
