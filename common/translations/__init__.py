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
)

__all__ = [
    "AnthropicStreamState",
    "parse_dsml_from_text",
    "repair_orphan_tool_messages",
    "strip_cache_control",
    "normalize_upstream_error",
]
