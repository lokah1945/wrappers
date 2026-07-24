"""Typed model-domain contracts shared by every wrapper."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ErrorState(str, Enum):
    AVAILABLE = "available"
    CATALOG_LISTED = "catalog_listed"
    UNKNOWN = "unknown"
    ACCOUNT_UNAVAILABLE = "account_unavailable"
    ACCOUNT_FORBIDDEN = "account_forbidden"
    INVALID_CREDENTIAL = "invalid_credential"
    KEY_RATE_LIMITED = "key_rate_limited"
    MODEL_RATE_LIMITED = "model_rate_limited"
    UPSTREAM_CAPACITY = "upstream_capacity"
    WRONG_ROUTE = "wrong_route"
    CAPABILITY_MISMATCH = "capability_mismatch"
    INVALID_PARAMETER = "invalid_parameter"
    TRANSIENT_FAILURE = "transient_failure"
    NETWORK_TIMEOUT = "network_timeout"
    GLOBALLY_RETIRED = "globally_retired"
    DEPRECATED = "deprecated"


@dataclass(frozen=True)
class ModelRef:
    requested_name: str
    canonical_id: str
    provider: str
    provider_model_id: str
    is_alias: bool = False
    resolution_revision: str = ""

    @property
    def alias_resolved(self) -> bool:
        return self.is_alias

    @property
    def model_changed(self) -> bool:
        """Alias resolution is not model substitution."""
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "alias_resolved": self.alias_resolved,
            "model_changed": False,
        }


@dataclass(frozen=True)
class CapabilityProfile:
    input_modalities: tuple[str, ...] = ("text",)
    output_modalities: tuple[str, ...] = ("text",)
    streaming: bool | None = None
    openai_chat: bool | None = None
    openai_responses: bool | None = None
    anthropic_messages: str | bool | None = None
    embeddings: bool | None = None
    vision: bool | None = None
    audio_input: bool | None = None
    audio_output: bool | None = None
    image_generation: bool | None = None
    tools: bool | None = None
    parallel_tools: bool | None = None
    structured_output: bool | None = None
    reasoning: bool | None = None
    thinking: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LimitProfile:
    context_window: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_tools: int | None = None
    max_images: int | None = None
    max_request_bytes: int | None = None
    max_stream_duration_sec: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProtocolProfile:
    client_surface: str
    upstream_surface: str
    path: str
    adapter_name: str
    adapter_version: str = "1"
    model_field: str = "model"
    streaming: bool | None = None
    base_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelProfile:
    canonical_id: str
    provider: str
    provider_model_id: str
    lifecycle_state: str = "active"
    profile_revision: str = ""
    catalog_revision: str = ""
    capabilities: CapabilityProfile = field(default_factory=CapabilityProfile)
    limits: LimitProfile = field(default_factory=LimitProfile)
    protocols: tuple[ProtocolProfile, ...] = ()
    request_rules: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=lambda: {
        "transparent": True,
        "model_substitution": False,
        "provider_substitution": False,
        "key_rotation": True,
    })
    provenance: dict[str, Any] = field(default_factory=dict)

    def protocol_for(self, client_surface: str) -> ProtocolProfile | None:
        for protocol in self.protocols:
            if protocol.client_surface == client_surface:
                return protocol
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = self.capabilities.to_dict()
        data["limits"] = self.limits.to_dict()
        data["protocols"] = [p.to_dict() for p in self.protocols]
        return data


@dataclass(frozen=True)
class CallPlan:
    model: ModelRef
    client_surface: str
    upstream_surface: str
    path: str
    adapter_name: str
    adapter_version: str
    model_field: str = "model"
    timeout_class: str = "default"
    key_rotation_allowed: bool = True
    model_substitution_allowed: bool = False
    parameter_rules: dict[str, Any] = field(default_factory=dict)
    base_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model"] = self.model.to_dict()
        return data


@dataclass(frozen=True)
class AliasBinding:
    scope_type: str
    scope_id: str
    alias: str
    canonical_target: str
    revision: str = ""
    source: str = "manifest"

    @property
    def scope_key(self) -> str:
        return f"{self.scope_type}:{self.scope_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"scope_key": self.scope_key}


@dataclass(frozen=True)
class AvailabilityObservation:
    provider: str
    endpoint: str
    canonical_model_id: str
    account_scope_hash: str
    credential_scope_hash: str
    state: ErrorState | str
    http_status: int = 0
    reason_code: str = ""
    reason_detail: str = ""
    checked_at: float = 0.0
    confidence: str = "observed"
    source: str = "runtime"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = str(self.state.value if isinstance(self.state, ErrorState) else self.state)
        return data


@dataclass(frozen=True)
class ErrorClassification:
    state: ErrorState | str
    reason_code: str
    retry_same_model: bool = False
    rotate_key: bool = False
    hard_block: bool = False
    account_scoped: bool = False

    def __getitem__(self, key: str):
        """Mapping compatibility for existing wrapper integrations."""
        if key == "state":
            return self.state.value if isinstance(self.state, ErrorState) else self.state
        if key == "reason_code":
            return self.reason_code
        if key in {"retry_same_model", "rotate_key", "hard_block", "account_scoped"}:
            return getattr(self, key)
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = str(self.state.value if isinstance(self.state, ErrorState) else self.state)
        return data
