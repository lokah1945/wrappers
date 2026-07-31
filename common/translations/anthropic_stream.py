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

Includes MiniMax DSML tool-call stripping to prevent leakage.
"""

from __future__ import annotations

import json
import re
import time
from typing import List, Optional


class AnthropicStreamState:
    """Converts OpenAI chat SSE chunks into Anthropic Messages SSE events.

    Handles reasoning, text, and tool calls. Strips leaked MiniMax DSML
    markup from content blocks.
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
        self.msg_id: str = f"msg_{int(time.time() * 1000)}"
        
        # DSML state
        self.in_dsml_mode = False
        self.dsml_buffer = ''
        self.current_tool_id = ''

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

    def _open_block(self, block_type: str, metadata: Optional[dict] = None) -> List[str]:
        """Open a new content block, closing the previous one if necessary."""
        if self.current_block == block_type:
            return []
        
        events = self._close_block()
        self.index += 1
        self.current_block = block_type
        
        content_block = {"type": block_type}
        if block_type == "thinking":
            content_block["thinking"] = ""
        elif block_type == "text":
            content_block["text"] = ""
        elif block_type == "tool_use" and metadata:
            content_block.update(metadata)
            
        events.append(self._sse("content_block_start", {
            "type": "content_block_start",
            "index": self.index,
            "content_block": content_block,
        }))
        return events

    def translate_chunk(self, chunk: dict) -> List[str]:
        """Translate one OpenAI chat SSE chunk into Anthropic events."""
        if self.finished:
            if isinstance(chunk, dict) and chunk.get("usage"):
                self.last_usage = chunk.get("usage") or {}
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

        # 1. Reasoning / thinking delta
        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            events.extend(self._open_block("thinking"))
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "thinking_delta", "thinking": reason},
            }))

        # 2. Text content delta (with DSML stripping)
        content = delta.get("content")
        if isinstance(content, str) and content:
            # Phase 2.4: MiniMax DSML leak prevention
            if self.in_dsml_mode or "DSML" in content.replace("\uff5c", "|"):
                events.extend(self._process_dsml_content(content))
            else:
                events.extend(self._open_block("text"))
                events.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "text_delta", "text": content},
                }))

        # 3. Tool calls delta (structured)
        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            fn = tc.get("function") or {}
            if oi not in self.tool_map:
                tid = tc.get("id") or f"toolu_{int(time.time() * 1000)}_{oi}"
                events.extend(self._open_block("tool_use", {
                    "id": tid,
                    "name": fn.get("name") or "",
                    "input": {},
                }))
                self.tool_map[oi] = self.index
                
            tidx = self.tool_map[oi]
            if fn.get("arguments"):
                events.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tidx,
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                }))

        # 4. Finish reason → terminal events
        fr = ch.get("finish_reason")
        if fr and not self.finished:
            events.extend(self.force_done(
                "tool_use" if (fr == "tool_calls" or self.tool_map or self.in_dsml_mode) else (
                    {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(fr, "end_turn")
                )
            ))

        return events

    def _process_dsml_content(self, content: str) -> List[str]:
        """Internal helper to strip DSML and extract tool calls."""
        events = []
        self.dsml_buffer += content
        
        while True:
            normalized = self.dsml_buffer.replace('\uff5c', '|').replace('<|DSML|', '|DSML|')
            
            # Match complete invoke block
            invoke_pair = re.search(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|invoke>', normalized)
            if invoke_pair:
                tool_name = invoke_pair.group(1)
                inner = invoke_pair.group(2)
                pair_end = invoke_pair.end()
                
                tid = f'toolu_dsml_{int(time.time() * 1000)}_{hash(tool_name) % 10000:04x}'
                events.extend(self._open_block("tool_use", {
                    "id": tid,
                    "name": tool_name,
                    "input": {},
                }))
                
                # Extract parameters
                params = {}
                for param_match in re.finditer(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|parameter>', inner):
                    params[param_match.group(1)] = param_match.group(2)
                
                events.append(self._sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self.index,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(params)},
                }))
                
                # Immediately close tool_use block after complete DSML invoke
                events.extend(self._close_block())
                
                self.dsml_buffer = self.dsml_buffer[pair_end:]
                continue
                
            # Transition into DSML mode
            if not self.in_dsml_mode:
                start_match = re.search(r'\|DSML\|tool_calls>', normalized)
                if start_match:
                    # Emit any text before the tag
                    before = self.dsml_buffer[:start_match.start()]
                    if before.strip():
                        events.extend(self._open_block("text"))
                        events.append(self._sse("content_block_delta", {
                            "type": "content_block_delta",
                            "index": self.index,
                            "delta": {"type": "text_delta", "text": before},
                        }))
                    
                    self.in_dsml_mode = True
                    self.dsml_buffer = self.dsml_buffer[start_match.end():]
                    continue
            
            # Transition out of DSML mode
            if self.in_dsml_mode:
                end_match = re.search(r'</\|DSML\|tool_calls>', normalized)
                if end_match:
                    self.in_dsml_mode = False
                    after = self.dsml_buffer[end_match.end():]
                    self.dsml_buffer = ''
                    if after:
                        # Recursively process remaining content
                        events.extend(self._process_dsml_content(after))
                    break
            
            # If we are in DSML mode but no complete invoke yet, just wait for more data
            if self.in_dsml_mode:
                break
                
            # Not in DSML mode and no new tags found → emit as normal text
            events.extend(self._open_block("text"))
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "text_delta", "text": self.dsml_buffer},
            }))
            self.dsml_buffer = ''
            break
            
        return events

    def force_done(self, stop: str = "end_turn") -> List[str]:
        """Emit terminal events if stream ended without finish_reason."""
        if self.finished:
            return []
        self.finished = True
        events: List[str] = []
        if not self.message_started:
            events.extend(self.start_events())
        
        # Flush remaining DSML buffer as text if not in DSML mode
        if not self.in_dsml_mode and self.dsml_buffer:
            events.extend(self._open_block("text"))
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta",
                "index": self.index,
                "delta": {"type": "text_delta", "text": self.dsml_buffer},
            }))
            self.dsml_buffer = ''
            
        events.extend(self._close_block())
        
        if (self.tool_map or self.in_dsml_mode) and stop == "end_turn":
            stop = "tool_use"
            
        usage = self.last_usage or {}
        events.append(self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {
                "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0,
                "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }))
        events.append(self._sse("message_stop", {"type": "message_stop"}))
        return events

    # Alias used by some wrappers
    done = force_done

    # Alias used by some wrappers
    done = force_done
