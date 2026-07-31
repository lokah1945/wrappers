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
import logging
import time
from typing import List, Optional

_log = logging.getLogger("wrapper-anthropic-stream")

# B-06 fix: strict OpenAI finish_reason → Anthropic stop_reason mapping.
# Previously `tool_use` was forced whenever ANY tool had been seen in the turn,
# so a turn that called a tool and then finished with "stop" or "length" still
# reported tool_use — Claude Code then waited forever for a tool_result that
# would never be requested, and genuine max_tokens truncation was masked.
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


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
        # CM-6: retain the last usage seen so force_done can report real
        # numbers instead of zeros on abnormal termination.
        self.last_usage: dict = {}
        # B-05: observability counter for content dropped after finish_reason.
        self.dropped_after_finish: int = 0
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
        # CM-6 fix: some providers send trailing chunks (usage-only or
        # content) after finish_reason. Emitting new content_block events
        # after message_stop violates the Anthropic protocol — but still
        # retain any late usage for observability before dropping.
        if self.finished:
            if isinstance(chunk, dict) and chunk.get("usage"):
                self.last_usage = chunk.get("usage") or {}
            # B-05 fix: dropping post-finish content is protocol-correct, but
            # it was previously SILENT — a truncated answer was indistinguishable
            # from a complete one. Count and log so truncation is observable.
            try:
                _d = ((chunk.get("choices") or [{}])[0].get("delta") or {}) if isinstance(chunk, dict) else {}
                if _d.get("content") or _d.get("reasoning_content") or _d.get("reasoning") or _d.get("tool_calls"):
                    self.dropped_after_finish += 1
                    _log.warning(
                        "[anthropic_stream] dropped content chunk received AFTER finish_reason "
                        "(model=%s, total_dropped=%d) — upstream is still emitting past its own "
                        "terminal signal; the client answer may be truncated.",
                        self.model, self.dropped_after_finish,
                    )
            except Exception:
                pass
            return []
        events = self.start_events()
        if not isinstance(chunk, dict) or "choices" not in chunk:
            if isinstance(chunk, dict) and chunk.get("usage"):
                self.last_usage = chunk.get("usage") or {}
            return events

        if chunk.get("usage"):
            self.last_usage = chunk.get("usage") or {}
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
            # B-06 fix: map STRICTLY from finish_reason. Do not infer tool_use
            # merely because self.tool_map is non-empty.
            stop = _FINISH_TO_STOP.get(fr, "end_turn")
            usage = chunk.get("usage") or self.last_usage or {}
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
        # Capture BEFORE _close_block() clears it.
        was_in_tool_block = (self.current_block == "tool_use")
        events.extend(self._close_block())
        # B-06 note: inferring tool_use from tool state is legitimate HERE and
        # only here — force_done() runs when the upstream ended WITHOUT any
        # finish_reason, so there is no authoritative signal to respect. The
        # bug fixed in translate_chunk() was overriding an explicit
        # finish_reason. Narrow the heuristic to the case where the stream died
        # while a tool_use block was still open (arguments mid-flight).
        if stop == "end_turn" and self.tool_map and was_in_tool_block:
            stop = "tool_use"
        usage = self.last_usage or {}
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

    # Alias used by some wrappers
    done = force_done
