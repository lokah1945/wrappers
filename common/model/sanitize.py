"""Sanitize provider error details before persistence or central publication."""

from __future__ import annotations

import json
import re
from typing import Any

_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+|(?:nvapi|sk|ghp|github_pat)-)[A-Za-z0-9_\-\.]+"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|credential|prompt|messages?|request[_-]?body|input|content)"
)
_ALLOWED_TOP_LEVEL = {
    "status", "title", "detail", "message", "code", "type", "error",
    "request_id", "requestId", "trace_id", "traceId", "retry_after",
}


def _scrub(value: Any, top_level: bool = False) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text) or (top_level and key_text not in _ALLOWED_TOP_LEVEL):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _scrub(child, False)
        return result
    if isinstance(value, list):
        return [_scrub(child, False) for child in value[:32]]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(r"\1[REDACTED]", value[:4000])
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4000]


def sanitize_error_detail(payload: Any, max_chars: int = 4000) -> str:
    """Return bounded, JSON-safe, credential/content-redacted diagnostics."""
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            scrubbed = _scrub(parsed, top_level=True)
        except (TypeError, ValueError):
            scrubbed = _SECRET_VALUE.sub(r"\1[REDACTED]", payload[:max_chars])
    else:
        scrubbed = _scrub(payload, top_level=True)
    try:
        return json.dumps(scrubbed, ensure_ascii=False, default=str)[:max_chars]
    except Exception:
        return str(scrubbed)[:max_chars]
