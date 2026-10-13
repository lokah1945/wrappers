"""Numeric limits observed in provider catalogs, never inferred from model names.

Keep the original catalog entry intact. Normalized fields are accompanied by
their source, scope and fetch time; unknown is not a conservative model limit.
Catalog limits describe advertised capacity, not a guarantee for every routed
backend, credential, request or reasoning budget.
"""
from __future__ import annotations

import copy
import time
from typing import Any


def positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


_FIELDS = {
    'context_window': ('context_length', 'context_window', 'max_context_length',
                       'max_context_window', 'max_model_len', 'context'),
    'max_input_tokens': ('max_input_tokens', 'max_prompt_tokens'),
    'max_output_tokens': ('max_output_tokens', 'max_completion_tokens',
                          'max-output', 'max_output', 'maxOutput'),
}


def extract_limits(entry: dict) -> tuple[dict, dict]:
    """Extract only explicit numeric fields; max_tokens alone is ambiguous."""
    limits, fields = {}, {}
    if not isinstance(entry, dict):
        return limits, fields
    containers = [('', entry)]
    for name in ('limits', 'top_provider'):
        if isinstance(entry.get(name), dict):
            containers.append((name + '.', entry[name]))
    for field, aliases in _FIELDS.items():
        for prefix, container in containers:
            for alias in aliases:
                value = positive_int(container.get(alias))
                if value is not None:
                    limits[field] = value
                    fields[field] = prefix + alias
                    break
            if field in limits:
                break
    return limits, fields


def annotate_catalog(entry: dict, source: str, *, checked_at: float | None = None,
                     ttl_sec: float = 21600, scope: str = 'provider_catalog') -> dict:
    result = copy.deepcopy(entry)
    limits, fields = extract_limits(entry)
    checked = time.time() if checked_at is None else checked_at
    result['context_metadata'] = {
        **limits, 'fields': fields, 'source': source, 'scope': scope,
        'checked_at': checked, 'expires_at': checked + max(1, ttl_sec),
        'status': 'known' if 'context_window' in limits else 'unknown',
    }
    return result


def context_metadata(entry: dict | None) -> dict:
    metadata = copy.deepcopy((entry or {}).get('context_metadata') or {})
    if not metadata:
        # Legacy caches have no verifiable fetch time. Do not manufacture one.
        limits, fields = extract_limits(entry or {})
        metadata = {**limits, 'fields': fields, 'source': 'legacy_provider_catalog',
                    'scope': 'provider_catalog', 'checked_at': None, 'expires_at': None,
                    'status': 'known' if 'context_window' in limits else 'unknown'}
    expiry = metadata.get('expires_at')
    metadata['stale'] = not isinstance(expiry, (int, float)) or expiry <= time.time()
    return metadata


def discovery_limits(entry: dict | None) -> dict:
    """Consistent discovery aliases used by different clients, with provenance."""
    metadata = context_metadata(entry)
    result = {'context_metadata': metadata}
    window = positive_int(metadata.get('context_window'))
    if window is not None:
        result.update(context_window=window, context_length=window, max_context_window=window)
    for key in ('max_input_tokens', 'max_output_tokens'):
        value = positive_int(metadata.get(key))
        if value is not None:
            result[key] = value
    return result


def merge_context_metadata(primary: dict | None, fallback: dict | None) -> dict:
    """Endpoint catalog fields win; published catalog fills only missing fields."""
    first, second = context_metadata(primary), context_metadata(fallback)
    selected = (first if positive_int(first.get('context_window')) or
                not positive_int(second.get('context_window')) else second)
    result = copy.deepcopy(selected)
    result['field_provenance'] = {}
    for key in _FIELDS:
        source = first if positive_int(first.get(key)) else second
        value = positive_int(source.get(key))
        if value is not None:
            result[key] = value
            result['field_provenance'][key] = {
                name: source.get(name) for name in
                ('source', 'scope', 'checked_at', 'expires_at', 'stale')}
    result['status'] = 'known' if positive_int(result.get('context_window')) else 'unknown'
    return result
