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

try:
    from common.sanitize_tokens import new_filter as _new_token_filter
    from common.sanitize_tokens import new_dsml_filter as _new_dsml_filter
except Exception:  # pragma: no cover - standalone use fallback
    def _new_token_filter():  # type: ignore[misc]
        class _Passthrough:
            def feed(self, t):
                return t

            def flush(self):
                return ''
        return _Passthrough()

    def _new_dsml_filter():  # type: ignore[misc]
        class _PassthroughDsml:
            collected_text = ''

            def feed(self, t):
                return t

            def flush(self):
                return ''
        return _PassthroughDsml()

try:
    from common.translations.shared import parse_dsml_from_text as _parse_dsml
except Exception:  # pragma: no cover
    _parse_dsml = None

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
        # R-02: Anthropic block indices of tool_use blocks that are still open.
        # Parallel tool calls must stay open concurrently.
        self.open_tool_blocks: set[int] = set()
        self.finished: bool = False
        # CM-6: retain the last usage seen so force_done can report real
        # numbers instead of zeros on abnormal termination.
        self.last_usage: dict = {}
        # B-05: observability counter for content dropped after finish_reason.
        self.dropped_after_finish: int = 0
        # R-03: set when the upstream reported a mid-stream error frame.
        self.upstream_error: Optional[str] = None
        self.msg_id: str = f"msg_{int(time.time() * 1000)}"
        # P0-4: stateful special-token scrubbers (one per channel so a token
        # fragmented across chunks — e.g. '<un' + 'k>' — is still caught).
        # Flushed via _close_block so remainder text lands in its own channel.
        self._tok_text = _new_token_filter()
        self._tok_reason = _new_token_filter()
        # R5 audit: stateful MiniMax DSML markup suppressor on the visible
        # text channel (cross-chunk). Complete markup segments are collected
        # and re-emitted as real tool_use blocks at stream end — parity with
        # the non-streaming openai_to_anthropic translator.
        self._dsml_text = _new_dsml_filter()

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
        """Close the currently open non-tool content block.

        Tool blocks are tracked separately in `open_tool_blocks` because
        parallel tool calls must remain open CONCURRENTLY — see
        `_close_all_tool_blocks`. P0-4: any text withheld by the special-token
        filter is flushed into its own channel before the block closes.
        """
        if self.current_block is None or self.current_block == "tool_use":
            return []
        ev: List[str] = []
        if self.current_block == "thinking":
            rest = self._tok_reason.flush()
            if rest:
                ev.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "thinking_delta", "thinking": rest},
                }))
        elif self.current_block == "text":
            rest = self._tok_text.flush()
            if rest:
                ev.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "text_delta", "text": rest},
                }))
        ev.append(self._sse("content_block_stop", {
            "type": "content_block_stop",
            "index": self.index,
        }))
        self.current_block = None
        return ev

    def _close_all_tool_blocks(self) -> List[str]:
        """Close every open tool_use block, lowest index first.

        RUNTIME FINDING R-02: the old code called `_close_block()` when opening
        the *next* tool, so with two parallel tool calls the sequence was

            start(0) delta(0) STOP(0) start(1) delta(1) delta(0) delta(1) stop(1)

        i.e. `content_block_delta` arrived on index 0 AFTER it was closed. That
        is an Anthropic protocol violation: the SDK/Claude Code either raises or
        discards the block, so the first tool's arguments are lost and the
        agent's tool call silently never executes. OpenAI interleaves argument
        fragments across ALL active tool indices, so concurrent tool blocks are
        the norm, not an edge case.
        """
        evs: List[str] = []
        for idx in sorted(self.open_tool_blocks):
            evs.append(self._sse("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            }))
        self.open_tool_blocks.clear()
        if self.current_block == "tool_use":
            self.current_block = None
        return evs

    def _close_everything(self) -> List[str]:
        """Close the open text/thinking block AND all open tool blocks."""
        return self._close_block() + self._close_all_tool_blocks()

    def _drain_terminal_addons(self) -> tuple:
        """R5 audit — terminal-time drainage, returns (events, dsml_tool_count).

        1. Flush DSML-withheld clean text (a potential partial opener that
            never grew into markup) into the text channel as a proper block,
            opening a fresh text block when the previous one already closed.
        2. Re-emit complete DSML tool markup collected mid-stream as real
           Anthropic tool_use blocks — parity with the non-streaming
           openai_to_anthropic translator, which parses the same markup via
           common.translations.parse_dsml_from_text. Without this, MiniMax
           tool calls over a STREAM silently vanished (the old per-chunk
           'DSML in chunk' drop), while the non-streaming path preserved
           them — a surface inconsistency that stalls agents mid-run.
        """
        ev: List[str] = []
        pre = self._dsml_text.flush()
        if pre:
            self._tok_text.feed(pre)
        rest = self._tok_text.flush()
        if rest:
            if self.current_block != 'text' or self.open_tool_blocks:
                ev.extend(self._close_everything())
                self.index += 1
                ev.append(self._sse('content_block_start', {
                    'type': 'content_block_start', 'index': self.index,
                    'content_block': {'type': 'text', 'text': ''}}))
                self.current_block = 'text'
            ev.append(self._sse('content_block_delta', {
                'type': 'content_block_delta', 'index': self.index,
                'delta': {'type': 'text_delta', 'text': rest}}))
        tools: list = []
        markup = getattr(self._dsml_text, 'collected_text', '') or ''
        if markup and _parse_dsml is not None:
            try:
                _clean, tools = _parse_dsml(markup)
            except Exception:
                tools = []
        for tu in tools:
            if not isinstance(tu, dict):
                continue
            ev.extend(self._close_everything())
            self.index += 1
            try:
                args_json = json.dumps(tu.get('input') or {}, ensure_ascii=False)
            except Exception:
                args_json = '{}'
            ev.append(self._sse('content_block_start', {
                'type': 'content_block_start', 'index': self.index,
                'content_block': {
                    'type': 'tool_use',
                    'id': tu.get('id') or f'toolu_dsml_{self.index}',
                    'name': tu.get('name') or '', 'input': {},
                }}))
            ev.append(self._sse('content_block_delta', {
                'type': 'content_block_delta', 'index': self.index,
                'delta': {'type': 'input_json_delta', 'partial_json': args_json}}))
            self.open_tool_blocks.add(self.index)
            self.current_block = 'tool_use'
        return ev, len(tools)

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

        # RUNTIME FINDING R-03: an upstream that reports a mid-stream failure as
        # a `{"error": {...}}` SSE frame was silently DISCARDED here (it has no
        # "choices" key), and the stream then closed with a fabricated
        # stop_reason:end_turn. The client saw a truncated answer as a complete,
        # successful turn — it could neither detect the failure nor retry.
        # Surface it as a real Anthropic `error` event and terminate.
        if isinstance(chunk, dict) and chunk.get("error") is not None and "choices" not in chunk:
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            etype = (err.get("type") if isinstance(err, dict) else None) or "api_error"
            self.upstream_error = msg or "upstream error"
            _log.error("[anthropic_stream] upstream error frame (model=%s): %s", self.model, msg)
            events.extend(self._close_everything())
            events.append(self._sse("error", {
                "type": "error",
                "error": {"type": etype, "message": str(msg)[:2000]},
            }))
            # Audit 2026-08-03: stop_reason=None on failure — fabricating
            # `end_turn` claims the turn completed cleanly. None is schema-
            # valid (stop_reason is Optional) and the `error` event already
            # signalled failure to strict clients; message_stop still follows
            # so lenient clients never hang.
            events.extend(self._terminal_events(None))
            self.finished = True
            return events

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
            # P0-4: scrub tokenizer specials (incl. cross-chunk fragments).
            reason = self._tok_reason.feed(reason)
            if reason:
                if self.current_block != "thinking":
                    # P3-4 fix: close only the current text/thinking block;
                    # tool blocks stay OPEN (R-02) — a reasoning blip between
                    # tool-argument fragments must not orphan those fragments.
                    events.extend(self._close_block())
                    self.index += 1
                    events.append(self._sse("content_block_start", {
                        "type": "content_block_start",
                        "index": self.index,
                        # P1-2: Anthropic thinking blocks carry a signature
                        # field (strict SDK validation requires it).
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
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
            # R5 audit: suppress DSML markup first (cross-chunk), then P0-4
            # scrub tokenizer specials (incl. cross-chunk fragments).
            content = self._tok_text.feed(self._dsml_text.feed(content))
            if content:
                if self.current_block != "text":
                    # P3-4 fix: close only the current text/thinking block (see
                    # reasoning branch above); tool blocks stay open.
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
                # R-02: close only text/thinking here. Closing the previous
                # TOOL block would orphan its later argument fragments.
                events.extend(self._close_block())
                self.index += 1
                self.tool_map[oi] = self.index
                self.open_tool_blocks.add(self.index)
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
            events.extend(self._close_everything())
            # R5 audit: drain DSML-withheld text + re-emit recovered DSML
            # tool_use blocks BEFORE the terminal frames.
            addons, _dsml_tool_n = self._drain_terminal_addons()
            events.extend(addons)
            if _dsml_tool_n:
                events.extend(self._close_everything())
            # B-06 fix: map STRICTLY from finish_reason. Do not infer tool_use
            # merely because self.tool_map is non-empty.
            stop = _FINISH_TO_STOP.get(fr, "end_turn")
            # R5 audit: DSML markup is the ONLY tool-call signal MiniMax
            # emits — the turn reports finish 'stop'. With recovered tool_use
            # blocks emitted above, end_turn would make the agent close the
            # turn and never execute the tool (non-stream shared translator
            # parity, CONTRACT §8). force_done() already upgrades the same way.
            if _dsml_tool_n and stop == "end_turn":
                stop = "tool_use"
            events.extend(self._terminal_events(stop, chunk.get("usage")))

        return events

    def _terminal_events(self, stop: str, usage: Optional[dict] = None) -> List[str]:
        """message_delta + message_stop, emitted exactly once."""
        u = usage or self.last_usage or {}
        return [
            self._sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {
                    "input_tokens": u.get("prompt_tokens", 0) or 0,
                    "output_tokens": u.get("completion_tokens", 0) or 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }),
            self._sse("message_stop", {"type": "message_stop"}),
        ]

    def force_done(self, stop: str = "end_turn") -> List[str]:
        """Emit terminal events if stream ended without finish_reason."""
        if self.finished:
            return []
        self.finished = True
        events: List[str] = []
        if not self.message_started:
            events.extend(self.start_events())
        # Capture BEFORE _close_block() clears it.
        was_in_tool_block = (self.current_block == "tool_use") or bool(self.open_tool_blocks)
        events.extend(self._close_everything())
        # R5 audit: drain DSML-withheld text + re-emit recovered DSML
        # tool_use blocks BEFORE the terminal frames.
        addons, dsml_tool_n = self._drain_terminal_addons()
        events.extend(addons)
        if dsml_tool_n:
            events.extend(self._close_everything())
            was_in_tool_block = True
        # B-06 note: inferring tool_use from tool state is legitimate HERE and
        # only here — force_done() runs when the upstream ended WITHOUT any
        # finish_reason, so there is no authoritative signal to respect. The
        # bug fixed in translate_chunk() was overriding an explicit
        # finish_reason. Narrow the heuristic to the case where the stream died
        # while a tool_use block was still open (arguments mid-flight).
        if stop == "end_turn" and (self.tool_map and was_in_tool_block or dsml_tool_n):
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
