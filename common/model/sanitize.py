"""Sanitize provider error details before persistence or central publication."""

from __future__ import annotations

import json
import math
import re
from typing import Any

_SECRET_VALUE = re.compile(
    r"(?i)(bearer\s+|(?:nvapi|sk|ghp|github_pat)[_\-])[A-Za-z0-9_\-\.]+"
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
    if isinstance(value, float):
        # BUG-ECB3 fix: NaN/Infinity are not valid JSON (RFC 7159).
        # Replace with None to ensure strict JSON parsers can handle output.
        import math
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, str):
        return _SECRET_VALUE.sub(r"\1[REDACTED]", value[:4000])
    if isinstance(value, (int, bool)) or value is None:
        return value
    return str(value)[:4000]


def sanitize_error_detail(payload: Any, max_chars: int = 4000) -> str:
    """Return bounded, JSON-safe, credential/content-redacted diagnostics.

    B-24.1: never raise — a pathological (>1000-deep) nested payload made
    json.loads/_scrub raise RecursionError, escaping the (TypeError,
    ValueError) guard and breaking the caller's error-record path exactly
    when the wrapper was already under upstream distress (§3.3)."""
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            scrubbed = _scrub(parsed, top_level=True)
        except (TypeError, ValueError, RecursionError):
            scrubbed = _SECRET_VALUE.sub(r"\1[REDACTED]", payload[:max_chars])
    else:
        try:
            scrubbed = _scrub(payload, top_level=True)
        except RecursionError:
            # NB: do NOT str(payload) here — repr of a pathologically nested
            # structure recurses too and would re-raise inside the handler.
            scrubbed = f"[UNSERIALIZABLE {type(payload).__name__}: recursion limit]"
    try:
        return json.dumps(scrubbed, ensure_ascii=False, default=str)[:max_chars]
    except Exception:
        return str(scrubbed)[:max_chars]


def sanitize_nonfinite_numbers(payload: Any) -> Any:
    """Replace NaN/±Infinity floats with None, iteratively, in place.

    B-36.1: Python's json.loads ACCEPTS the literals ``NaN``/``Infinity``/
    ``-Infinity``. An upstream response body carrying one parsed fine, then
    crashed the Starlette render (``allow_nan=False`` → ValueError → an
    unhandled 500 on an otherwise-successful turn — CONTRACT §3.3 inversion),
    or was re-serialized with the RFC-invalid ``NaN`` literal that strict
    client SDKs reject mid-stream. Apply at the upstream-ingest boundary so
    every downstream surface stays RFC-valid. The walk is iterative
    (stack-free) — immune to pathological nesting depth, and dicts/lists are
    only visited exactly once (parsed JSON is acyclic).
    """
    if isinstance(payload, float):
        return payload if math.isfinite(payload) else None
    if isinstance(payload, (dict, list)):
        # Single stack, typed nodes: dict mutates by key, list by index.
        # (The first version popped list frames through dict.items() — the
        # unit test caught it: mixed containers are the norm.)
        stack = [payload]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for key, child in node.items():
                    if isinstance(child, float):
                        if not math.isfinite(child):
                            node[key] = None
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            else:
                for idx, child in enumerate(node):
                    if isinstance(child, float):
                        if not math.isfinite(child):
                            node[idx] = None
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
        return payload
    return payload
