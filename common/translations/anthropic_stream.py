#!/usr/bin/env python3
"""Shared AnthropicStreamState — OpenAI chat SSE → Anthropic Messages SSE.

Extracted from duplicated code in nvidia-python, opencode, and blackbox.
Nous uses a dict-based variant (see AnthropicStreamStateDict below for
compatibility); all other wrappers use this string-based implementation.

Lifecycle:
  message_start → content_block_start/delta/stop (per block) →
  message_delta → message_stop

Block types: thinking, text, tool_use. Blocks are closed and reopened
as the upstream stream transitions between them.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional


class AnthropicStreamState:
    """Converts OpenAI chat SSE chunks into Anthropic Messages SSE events.

    Usage:
        state = AnthropicStreamState(model="meta/llama-3.1-8b-instruct")
        for chunk in openai_sse_chunks:
            events = state.translate_chunk(chunk)
            for event_str in events:
                yield event_str
        for event_str in state.force_done():
            yield event_str
    """

    def __init__(self, model: str):
        self.model = model
        self.index: int = -1
        self.message_started: bool = False
        self.current_block: Optional[str] = None  # 'thinking' | 'text' | 'tool_use'
        self.tool_map: dict[int, int] = {}
        self.finished: bool = False
        self.msg_id: str = f"msg_{int(time.time() * 1000)}"

    def _sse(self, event: str, data: dict) -> str:
        payload = dict(data)
        if "type" not in payload:
            payload["type"] = event
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def start_events(self) -> List[str]:
        """Emit message_start exactly once."""
        if self.message_started:
            return []
        self.message_started = True
        return [self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        })]

    def _close_block(self) -> List[str]:
        """Close the currently open content block."""
        if self.current_block is None:
            return []
        ev = [self._sse("content_block_stop", {
            "type": "content_block_stop",
            "index": self.index,
        })]
        self.current_block = None
        return ev

    def translate_chunk(self, chunk: dict) -> List[str]:
        """Translate one OpenAI chat SSE chunk into Anthropic events."""
        events = self.start_events()
        if not isinstance(chunk, dict) or "choices" not in chunk:
            return events

        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}

        # Reasoning / thinking delta
        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            if self.current_block != "thinking":
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {"type": "thinking", "thinking": ""},
                }))
                self.current_block = "thinking"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "thinking_delta", "thinking": reason},
            }))

        # Text content delta
        content = delta.get("content")
        if isinstance(content, str) and content:
            if self.current_block != "text":
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {"type": "text", "text": ""},
                }))
                self.current_block = "text"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "text_delta", "text": content},
            }))

        # Tool calls delta
        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            fn = tc.get("function") or {}
            if oi not in self.tool_map:
                events.extend(self._close_block())
                self.index += 1
                self.tool_map[oi] = self.index
                tid = tc.get("id") or f"toolu_{self.index}"
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start",
                    "index": self.index,
                    "content_block": {
                        "type": "tool_use",
                        "id": tid,
                        "name": fn.get("name") or "",
                        "input": {},
                    },
                }))
                self.current_block = "tool_use"
            if fn.get("arguments"):
                events.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.tool_map[oi],
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                }))

        # Finish reason → terminal events
        fr = ch.get("finish_reason")
        if fr and not self.finished:
            self.finished = True
            events.extend(self._close_block())
            stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else (
                {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(fr, "end_turn")
            )
            usage = chunk.get("usage") or {}
            events.append(self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0) or 0,
                    "output_tokens": usage.get("completion_tokens", 0) or 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }))
            events.append(self._sse("message_stop", {"type": "message_stop"}))

        return events

    def force_done(self, stop: str = "end_turn") -> List[str]:
        """Emit terminal events if stream ended without finish_reason."""
        if self.finished:
            return []
        self.finished = True
        events: List[str] = []
        if not self.message_started:
            events.extend(self.start_events())
        events.extend(self._close_block())
        if self.tool_map and stop == "end_turn":
            stop = "tool_use"
        events.append(self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }))
        events.append(self._sse("message_stop", {"type": "message_stop"}))
        return events

    # Alias used by some wrappers
    done = force_done
