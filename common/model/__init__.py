"""Shared model identity, capability, call-plan, and error contracts.

This package deliberately contains no model fallback or provider substitution
logic. It describes and executes the exact model selected by the client; the
wrapper's native key pool is the only retry dimension.
"""

from .call_plan import CallPlanError, build_call_plan
from .central_client import ModelRegistryClient
from .contracts import (
    AliasBinding,
    AvailabilityObservation,
    CallPlan,
    CapabilityProfile,
    ErrorClassification,
    LimitProfile,
    ModelProfile,
    ModelRef,
    ProtocolProfile,
)
from .errors import ErrorState, classify_upstream_error, error_text
from .identity import AliasResolver, normalize_model_syntax
from .profile_store import ModelProfileStore
from .registry import LocalModelRegistry
from .sanitize import sanitize_error_detail
from .validation import validate_catalog_entries, validate_model_id, validate_observation

__all__ = [
    "AliasBinding",
    "AliasResolver",
    "AvailabilityObservation",
    "CallPlan",
    "CallPlanError",
    "CapabilityProfile",
    "ErrorClassification",
    "ErrorState",
    "LimitProfile",
    "LocalModelRegistry",
    "ModelProfileStore",
    "ModelRegistryClient",
    "ModelProfile",
    "ModelRef",
    "ProtocolProfile",
    "build_call_plan",
    "classify_upstream_error",
    "error_text",
    "normalize_model_syntax",
    "sanitize_error_detail",
    "validate_catalog_entries",
    "validate_model_id",
    "validate_observation",
]
