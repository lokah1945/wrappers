"""Shared translation utilities for all wrappers.

Extracted from duplicated code across nvidia-python, nous, opencode, and blackbox.
All wrappers now import from here instead of maintaining independent copies.
"""

from .anthropic_stream import AnthropicStreamState
from .shared import (
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
    anthropic_to_openai_response,
    openai_to_anthropic_response,
    stream_anthropic_to_openai,
    openai_chat_to_anthropic_request,
    responses_usage,
    tokens_from_chat_usage,
    scrub_visible_text,
    responses_content_to_chat,
)

__all__ = [
    "AnthropicStreamState",
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
    "anthropic_to_openai_response",
    "openai_to_anthropic_response",
    "stream_anthropic_to_openai",
    "openai_chat_to_anthropic_request",
    "responses_usage",
    "tokens_from_chat_usage",
    "scrub_visible_text",
    "responses_content_to_chat",
]
