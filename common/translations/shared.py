#!/usr/bin/env python3
"""Shared translation utilities extracted from duplicated wrapper code.

These functions are identical (or near-identical) across nvidia-python,
nous, opencode, and blackbox wrappers. Centralizing them here means
bug fixes only need to be applied once.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, List


def parse_dsml_from_text(text: str) -> tuple[str, list[dict]]:
    """Split MiniMax DSML tool markup leaked into content into (clean_text, tool_use blocks).

    Some upstream models (particularly MiniMax) leak their internal DSML
    tool-call markup into the visible content stream. This function
    extracts structured tool_use blocks and returns clean text.

    Returns:
        (clean_text, tool_uses) where tool_uses is a list of Anthropic-format
        tool_use content blocks.
    """
    if not text or "DSML" not in str(text).replace("\uff5c", "|"):
        return text or "", []

    normalized = str(text).replace("\uff5c", "|").replace("<|DSML|", "|DSML|")
    if "|DSML|tool_calls>" not in normalized:
        return text, []

    tools: list[dict] = []
    clean_parts: list[str] = []
    OPEN = "|DSML|tool_calls>"
    CLOSE = "</|DSML|tool_calls>"
    cursor = 0

    while True:
        s_idx = normalized.find(OPEN, cursor)
        if s_idx == -1:
            clean_parts.append(normalized[cursor:])
            break
        if s_idx > cursor:
            clean_parts.append(normalized[cursor:s_idx])
        e_idx = normalized.find(CLOSE, s_idx)
        if e_idx == -1:
            # Incomplete DSML — don't leak partial markup
            clean_parts.append(normalized[s_idx:])
            break
        segment = normalized[s_idx:e_idx + len(CLOSE)]
        for name, inner in re.findall(
            r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|invoke>',
            segment,
        ):
            params = dict(re.findall(
                r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|parameter>',
                inner,
            ))
            tools.append({
                "type": "tool_use",
                "id": f"toolu_dsml_{int(time.time() * 1000)}_{hash(name) % 10000:04x}",
                "name": name,
                "input": params,
            })
        cursor = e_idx + len(CLOSE)

    return "".join(clean_parts).strip(), tools


def repair_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Convert orphan role=tool messages into user text.

    When conversation history is lost (process restart, missing
    previous_response_id), role=tool messages may reference tool_call_ids
    that don't exist in any preceding assistant message. Upstream APIs
    reject such sequences. This function converts orphans into user
    text messages as a last-resort recovery.
    """
    seen_call_ids: set[str] = set()
    repaired: list[dict] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    seen_call_ids.add(tc["id"])
            repaired.append(msg)
            continue

        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id") or ""
            if tcid and tcid in seen_call_ids:
                repaired.append(msg)
            else:
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                repaired.append({
                    "role": "user",
                    "content": f"Tool result{f' for {tcid}' if tcid else ''}: {content}",
                })
            continue

        repaired.append(msg)

    return repaired


def strip_cache_control(node: Any) -> Any:
    """Recursively remove cache_control keys from Anthropic request bodies.

    NVIDIA NIM and other upstreams don't support Anthropic's prompt caching
    annotations. Stripping them prevents upstream validation errors.
    """
    if isinstance(node, list):
        for item in node:
            strip_cache_control(item)
        return node
    if isinstance(node, dict):
        node.pop("cache_control", None)
        for k in list(node.keys()):
            v = node[k]
            if isinstance(v, (dict, list)):
                strip_cache_control(v)
    return node


def normalize_upstream_error(status: int, text_or_data: Any) -> dict:
    """Convert upstream error body into a single OpenAI-shaped error.

    Unwraps nested JSON string messages (some providers double-encode errors)
    and maps HTTP status codes to SDK-compatible error types.
    """
    msg: Any = text_or_data
    etype = "api_error"

    if isinstance(text_or_data, dict):
        if isinstance(text_or_data.get("error"), dict):
            err = text_or_data["error"]
            msg = err.get("message") or err.get("msg") or str(err)
            etype = err.get("type") or etype
        elif text_or_data.get("message"):
            msg = text_or_data.get("message")
            etype = text_or_data.get("type") or etype
        else:
            msg = json.dumps(text_or_data)[:2000]
    else:
        msg = str(text_or_data or "")
        try:
            parsed = json.loads(msg)
            return normalize_upstream_error(status, parsed)
        except Exception:
            pass

    if isinstance(msg, str):
        try:
            inner = json.loads(msg)
            if isinstance(inner, dict):
                if isinstance(inner.get("error"), dict):
                    msg = inner["error"].get("message") or msg
                    etype = inner["error"].get("type") or etype
                elif inner.get("message"):
                    msg = inner.get("message")
        except Exception:
            pass

    if status == 429:
        etype = "rate_limit_error"
    elif status in (401, 402, 403):
        etype = "authentication_error"
    elif status == 404:
        etype = "not_found_error"
    elif status >= 500:
        etype = "server_error"

    return {"error": {"message": str(msg)[:2000], "type": etype, "code": status}}
