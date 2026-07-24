"""Provider-independent error taxonomy with provider-specific text matching."""

from __future__ import annotations

import json
from typing import Any

from .contracts import ErrorClassification, ErrorState


def error_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload[:4000]
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    except Exception:
        return str(payload)[:4000]


def classify_upstream_error(status: int, payload: Any = "") -> ErrorClassification:
    """Classify an upstream result without inventing global model state.

    The retry dimension is a credential/key only. No result enables changing the
    requested model or provider.
    """
    text = error_text(payload)
    lower = text.lower()

    if status == 200 or (status and status < 400):
        return ErrorClassification(ErrorState.AVAILABLE, "OK")
    if status == 401:
        return ErrorClassification(
            ErrorState.INVALID_CREDENTIAL,
            "INVALID_CREDENTIAL",
            retry_same_model=True,
            rotate_key=True,
            account_scoped=True,
        )
    if status == 403:
        return ErrorClassification(
            ErrorState.ACCOUNT_FORBIDDEN,
            "AUTH_OR_PERMISSION",
            retry_same_model=True,
            rotate_key=True,
            account_scoped=True,
        )
    if status == 404:
        if "not found for account" in lower or ("function" in lower and "for account" in lower):
            return ErrorClassification(
                ErrorState.ACCOUNT_UNAVAILABLE,
                "NOT_DEPLOYED_FOR_ACCOUNT",
                retry_same_model=True,
                rotate_key=True,
                account_scoped=True,
            )
        if "page not found" in lower or "route" in lower:
            return ErrorClassification(ErrorState.WRONG_ROUTE, "UPSTREAM_ROUTE_NOT_FOUND")
        return ErrorClassification(ErrorState.UNKNOWN, "MODEL_NOT_FOUND_OR_UNAVAILABLE")
    if status == 410:
        if any(term in lower for term in ("end of life", "eol", "retired", "deprecated", "sunset")):
            return ErrorClassification(ErrorState.GLOBALLY_RETIRED, "PROVIDER_EOL", hard_block=True)
        return ErrorClassification(ErrorState.UNKNOWN, "HTTP_410_UNCONFIRMED")
    if status == 429:
        model_limited = any(term in lower for term in ("model", "deployment", "capacity"))
        if model_limited:
            return ErrorClassification(ErrorState.MODEL_RATE_LIMITED, "MODEL_OR_DEPLOYMENT_RATE_LIMIT")
        return ErrorClassification(
            ErrorState.KEY_RATE_LIMITED,
            "KEY_RATE_LIMIT",
            retry_same_model=True,
            rotate_key=True,
            account_scoped=True,
        )
    if status in (408, 425) or status == 0:
        return ErrorClassification(
            ErrorState.NETWORK_TIMEOUT if status in (0, 408) else ErrorState.TRANSIENT_FAILURE,
            "NETWORK_OR_TIMEOUT",
            retry_same_model=True,
            rotate_key=True,
        )
    if status >= 500:
        return ErrorClassification(
            ErrorState.TRANSIENT_FAILURE,
            "UPSTREAM_TRANSIENT",
            retry_same_model=True,
            rotate_key=True,
        )
    if status in (400, 422):
        return ErrorClassification(ErrorState.CAPABILITY_MISMATCH, "INVALID_REQUEST_OR_PARAMETER")
    return ErrorClassification(ErrorState.UNKNOWN, f"HTTP_{status}")
