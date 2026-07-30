"""Boundary validation for model catalog and observation payloads."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .contracts import ErrorState

MAX_CATALOG_ENTRIES = 10000
MAX_MODEL_ID_CHARS = 512
MAX_METADATA_BYTES = 65536
MAX_REASON_CHARS = 4000


def validate_provider_name(value: Any) -> str:
    provider = str(value or "").strip().lower()
    if not provider:
        raise ValueError("provider is required")
    if len(provider) > 64 or not provider.replace("-", "").replace("_", "").isalnum():
        raise ValueError("provider name contains invalid characters")
    return provider


def validate_model_id(value: Any) -> str:
    model_id = str(value or "").strip()
    if not model_id:
        raise ValueError("model id is required")
    if len(model_id) > MAX_MODEL_ID_CHARS:
        raise ValueError("model id is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in model_id):
        raise ValueError("model id contains control characters")
    if any(segment in {".", ".."} for segment in model_id.split("/")):
        raise ValueError("model id contains invalid path segment")
    return model_id


def validate_catalog_entries(models: Iterable[Any]) -> list[Any]:
    entries = list(models or [])
    if len(entries) > MAX_CATALOG_ENTRIES:
        raise ValueError("catalog contains too many models")
    for entry in entries:
        if isinstance(entry, str):
            validate_model_id(entry)
            continue
        if not isinstance(entry, dict):
            raise ValueError("catalog entry must be a model id or object")
        model_id = entry.get("id") or entry.get("model")
        validate_model_id(model_id)
        try:
            encoded = json.dumps(entry, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("catalog metadata is not serializable") from exc
        if len(encoded) > MAX_METADATA_BYTES:
            raise ValueError("catalog metadata is too large")
    return entries


def validate_observation(model_id: Any, account_scope: Any, state: Any,
                         reason_code: Any, reason_detail: Any, endpoint: Any) -> dict[str, str]:
    normalized_state = str(state or "unknown")
    allowed = {item.value for item in ErrorState}
    if normalized_state not in allowed and normalized_state != "mixed":
        raise ValueError(f"unknown model state: {normalized_state}")
    result = {
        "model_id": validate_model_id(model_id),
        "account_scope": str(account_scope or "unknown").strip()[:128],
        "state": normalized_state,
        "reason_code": str(reason_code or "")[:256],
        "reason_detail": str(reason_detail or "")[:MAX_REASON_CHARS],
        "endpoint": str(endpoint or "")[:512],
    }
    if not result["account_scope"]:
        result["account_scope"] = "unknown"
    return result
