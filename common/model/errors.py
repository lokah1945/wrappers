"""Provider-independent error taxonomy with provider-specific text matching."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .contracts import ErrorClassification, ErrorState


def error_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload[:4000]
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)[:4000]
    except RecursionError:
        # B-25.1: str()/repr() on a pathologically nested structure recurses
        # again — the naive fallback re-raised INSIDE the handler, crashing
        # error classification (classify_upstream_error) on the proxy error
        # path (§3.3). Use a fixed placeholder instead.
        return f"[UNSERIALIZABLE {type(payload).__name__}: recursion limit]"
    except Exception:
        try:
            return str(payload)[:4000]
        except RecursionError:
            return f"[UNSERIALIZABLE {type(payload).__name__}: recursion limit]"


def provider_account_hint(payload: Any) -> str:
    """Extract a provider account identifier hint without returning raw text."""
    text = error_text(payload)
    match = re.search(r"(?i)(?:account|tenant|project)[^A-Za-z0-9]+([A-Za-z0-9._:-]{3,128})", text)
    return match.group(1) if match else ""


_ANTI_BOT_HTML_PREFIXES = ("<!doctype", "<html", "<?xml")
_ANTI_BOT_MARKERS = (
    "cloudflare", "cf-ray", "cf-chl", "cf-connecting-ip",
    "used cloudflare to restrict access", "access denied",
    "captcha", "just a moment", "security check", "verify you are human",
)
# Strongly identifying markers that suffice on their own. Includes the exact
# phrase emitted by common.translations.normalize_upstream_error for sanitized
# anti-bot errors, so should_cooldown_key() still recognizes an anti-bot block
# after the raw HTML body has been normalized away.
_ANTI_BOT_STRONG_MARKERS = (
    "cf-ray", "cf-chl", "used cloudflare to restrict access",
    "anti-bot protection blocked the request",
)


def looks_anti_bot_challenge(payload: Any) -> bool:
    """Detect HTML anti-bot pages (Cloudflare et al) masquerading as API errors.

    These are transient transport blocks, NOT credential failures. Cooldown and
    account-state logic must not treat them as auth_or_quota (see F1 audit
    finding: a UA-based Cloudflare block previously cooled down every key for
    AUTH_KEY_COOLDOWN_SEC and leaked raw HTML into SDK error messages).
    """
    text = error_text(payload).lower().lstrip()
    if any(text.startswith(p) for p in _ANTI_BOT_HTML_PREFIXES):
        return True
    if any(m in text for m in _ANTI_BOT_STRONG_MARKERS):
        return True
    hits = [m for m in _ANTI_BOT_MARKERS if m in text]
    # A single generic marker (e.g. "access denied") is not enough — require
    # two corroborating markers.
    return len(hits) >= 2


def classify_provider_error(provider: str, status: int, payload: Any = "", manifest: dict[str, Any] | None = None) -> ErrorClassification:
    """Apply provider-specific manifest rules before shared classification."""
    text = error_text(payload).lower()
    for rule in (manifest or {}).get("rules", []):
        if int(rule.get("status", -1)) != int(status):
            continue
        needle = str(rule.get("body_contains") or "").lower()
        if needle and needle not in text:
            continue
        # Generic manifest rules without a body signature are deliberately
        # ignored here; the shared classifier has richer retry semantics.
        if not needle:
            continue
        try:
            state = ErrorState(str(rule.get("state")))
        except ValueError:
            continue
        return ErrorClassification(
            state=state,
            reason_code=str(rule.get("reason_code") or rule.get("state") or "PROVIDER_RULE"),
            retry_same_model=bool(rule.get("retry_same_model", False)),
            rotate_key=bool(rule.get("rotate_key", False)),
            hard_block=bool(rule.get("hard_block", False)),
            account_scoped=state in {ErrorState.ACCOUNT_UNAVAILABLE, ErrorState.ACCOUNT_FORBIDDEN},
        )
    return classify_upstream_error(status, payload)


def load_provider_error_manifest(provider: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "model-registry" / "manifests" / "errors" / f"{provider}.json"
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def classify_upstream_error(status: int, payload: Any = "") -> ErrorClassification:
    """Classify an upstream result without inventing global model state.

    The retry dimension is a credential/key only. No result enables changing the
    requested model or provider.
    """
    text = error_text(payload)
    lower = text.lower()

    if 200 <= status < 300:
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
        if looks_anti_bot_challenge(payload):
            # Anti-bot/Cloudflare transport block — NOT an authentication
            # failure. Transient: keep the retry loop alive across keys but
            # never rotate/punish credentials or poison account state (F1).
            return ErrorClassification(
                ErrorState.TRANSIENT_FAILURE,
                "ANTI_BOT_CHALLENGE",
                retry_same_model=True,
            )
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
            # NO MODEL FALLBACK: a model rate limit is per-key-per-model.
            # The rate limit is on THIS key for THIS model — another key may
            # still serve the same model. Retry the SAME model with a DIFFERENT
            # key (retry_same_model=True, rotate_key=True). Never substitute
            # model B for model A.
            return ErrorClassification(
                ErrorState.MODEL_RATE_LIMITED,
                "MODEL_OR_DEPLOYMENT_RATE_LIMIT",
                retry_same_model=True,
                rotate_key=True,
            )
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
