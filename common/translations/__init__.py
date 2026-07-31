"""Shared translation utilities for all wrappers.

Extracted from duplicated code across nvidia-python, nous, opencode, and blackbox.
All wrappers now import from here instead of maintaining independent copies.
"""

from .anthropic_stream import AnthropicStreamState
from .responses_stream import ResponsesStreamState
from .shared import (
    is_anthropic_message_order_valid,
    parse_dsml_from_text,
    repair_orphan_tool_messages,
    strip_cache_control,
    normalize_upstream_error,
    parse_retry_after,
    is_retriable_status,
    should_cooldown_key,
    build_forward_headers,
    sanitize_header_value,
    FORWARD_HEADER_ALLOWLIST,
    HOP_BY_HOP_HEADERS,
)

__all__ = [
    "AnthropicStreamState",
    "ResponsesStreamState",
    "is_anthropic_message_order_valid",
    "parse_dsml_from_text",
    "repair_orphan_tool_messages",
    "strip_cache_control",
    "normalize_upstream_error",
    "parse_retry_after",
    "is_retriable_status",
    "should_cooldown_key",
    "build_forward_headers",
    "sanitize_header_value",
    "FORWARD_HEADER_ALLOWLIST",
    "HOP_BY_HOP_HEADERS",
]
