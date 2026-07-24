"""Build one exact provider call plan from a model profile."""

from __future__ import annotations

from .contracts import CallPlan, ModelProfile


class CallPlanError(ValueError):
    pass


def build_call_plan(profile: ModelProfile, model_ref, client_surface: str) -> CallPlan:
    protocol = profile.protocol_for(client_surface)
    if protocol is None:
        raise CallPlanError(
            f"model {profile.canonical_id} does not support client surface {client_surface}"
        )
    if profile.policy.get("model_substitution", False):
        raise CallPlanError("invalid profile: model substitution is forbidden")
    if profile.policy.get("provider_substitution", False):
        raise CallPlanError("invalid profile: provider substitution is forbidden")
    if protocol.upstream_surface == "":
        raise CallPlanError(f"model {profile.canonical_id} has no upstream surface")
    return CallPlan(
        model=model_ref,
        client_surface=protocol.client_surface,
        upstream_surface=protocol.upstream_surface,
        path=protocol.path,
        adapter_name=protocol.adapter_name,
        adapter_version=protocol.adapter_version,
        model_field=protocol.model_field,
        timeout_class=str(profile.request_rules.get("timeout_class", "default")),
        key_rotation_allowed=bool(profile.policy.get("key_rotation", True)),
        model_substitution_allowed=False,
        parameter_rules=dict(profile.request_rules),
    )
