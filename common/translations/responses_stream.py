#!/usr/bin/env python3
"""Shared ResponsesStreamState — OpenAI chat SSE → OpenAI Responses SSE.

Converts standard OpenAI Chat Completions streaming chunks into the
OpenAI Responses API event lifecycle used by Codex, Claude Code, and
other agentic clients.

Lifecycle:
  response.created → response.in_progress → response.output_item.added →
  response.content_part.added → response.output_text.delta →
  response.output_text.done → response.content_part.done →
  response.output_item.done → response.completed

Handles:
  - Text deltas
  - Reasoning/thinking deltas (from reasoning_content or reasoning fields)
  - Tool call deltas (parallel support)
  - MiniMax DSML stripping from content deltas
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional


class ResponsesStreamState:
    """State machine for OpenAI Responses API streaming."""

    def __init__(self, response_id: str, model: str):
        self.rid = response_id
        self.model = model
        self.seq = 0
        self.started = False
        self.completed = False
        
        # Tracking items
        self.msg_id = f"msg_{int(time.time() * 1000)}"
        self.msg_index = 0
        self.text_started = False
        self.full_text = ""
        
        self.reasoning_started = False
        self.rsn_id = f"rsn_{int(time.time() * 1000)}"
        self.rsn_index = -1
        self.full_reasoning = ""
        
        self.tool_acc: Dict[int, Dict[str, Any]] = {}  # index -> {call_id, name, args, output_index}
        self.next_output_index = 1
        
        self.accum_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        
        # DSML state
        self.in_dsml_mode = False
        self.dsml_buffer = ""

    def _next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def _emit(self, event_type: str, payload: dict) -> str:
        """Build a complete SSE frame."""
        data = {"type": event_type, "sequence_number": self._next_seq(), **payload}
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def start_events(self) -> List[str]:
        """Emit the initial response events."""
        if self.started:
            return []
        self.started = True
        
        base = {
            "id": self.rid,
            "object": "response",
            "created_at": int(time.time()),
            "model": self.model,
            "status": "in_progress",
            "output": [],
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        }
        
        return [
            self._emit("response.created", {"response": base}),
            self._emit("response.in_progress", {"response": {"id": self.rid, "status": "in_progress"}}),
        ]

    def _ensure_message_item(self) -> List[str]:
        """Ensure the assistant message output item is added."""
        if self.text_started:
            return []
        self.text_started = True
        
        # Text usually takes index 0 in the output array
        events = [
            self._emit("response.output_item.added", {
                "output_index": self.msg_index,
                "item": {
                    "id": self.msg_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }),
            self._emit("response.content_part.added", {
                "item_id": self.msg_id,
                "output_index": self.msg_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }),
        ]
        return events

    def translate_chunk(self, chunk: dict) -> List[str]:
        """Translate one OpenAI chat SSE chunk into Responses events."""
        if self.completed:
            return []
            
        events = self.start_events()
        if not isinstance(chunk, dict) or "choices" not in chunk:
            if isinstance(chunk, dict) and chunk.get("usage"):
                self._update_usage(chunk["usage"])
            return events

        if chunk.get("usage"):
            self._update_usage(chunk["usage"])
            
        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}

        # 1. Reasoning / thinking delta
        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            events.extend(self._handle_reasoning_delta(reason))

        # 2. Text content delta (with DSML stripping)
        content = delta.get("content")
        if isinstance(content, str) and content:
            if self.in_dsml_mode or "DSML" in content.replace("\uff5c", "|"):
                events.extend(self._process_dsml_content(content))
            else:
                events.extend(self._handle_text_delta(content))

        # 3. Tool calls delta
        for tc in delta.get("tool_calls") or []:
            events.extend(self._handle_tool_delta(tc))

        # 4. Finish reason handled by the caller or force_done
        if ch.get("finish_reason"):
            # We don't call force_done automatically here because we might want 
            # to see the final usage chunk.
            pass

        return events

    def _update_usage(self, u: dict):
        p = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
        c = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
        self.accum_usage["input_tokens"] = int(p)
        self.accum_usage["output_tokens"] = int(c)
        self.accum_usage["total_tokens"] = int(p) + int(c)

    def _handle_reasoning_delta(self, delta: str) -> List[str]:
        events = []
        if not self.reasoning_started:
            self.reasoning_started = True
            self.rsn_index = self.next_output_index
            self.next_output_index += 1
            events.append(self._emit("response.output_item.added", {
                "output_index": self.rsn_index,
                "item": {
                    "id": self.rsn_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": "",
                    "content": [],
                },
            }))
        self.full_reasoning += delta
        events.append(self._emit("response.reasoning_text.delta", {
            "item_id": self.rsn_id,
            "output_index": self.rsn_index,
            "content_index": 0,
            "delta": delta,
        }))
        return events

    def _handle_text_delta(self, delta: str) -> List[str]:
        events = self._ensure_message_item()
        self.full_text += delta
        events.append(self._emit("response.output_text.delta", {
            "item_id": self.msg_id,
            "output_index": self.msg_index,
            "content_index": 0,
            "delta": delta,
        }))
        return events

    def _handle_tool_delta(self, tc: dict) -> List[str]:
        events = []
        idx = tc.get("index", 0)
        fn = tc.get("function") or {}
        
        if idx not in self.tool_acc:
            acc = {
                "call_id": tc.get("id") or f"call_{int(time.time() * 1000)}_{idx}",
                "name": "",
                "args": "",
                "output_index": self.next_output_index,
                "added": False
            }
            self.next_output_index += 1
            self.tool_acc[idx] = acc
            
        acc = self.tool_acc[idx]
        if tc.get("id"):
            acc["call_id"] = tc["id"]
        if fn.get("name"):
            acc["name"] += fn["name"]
            
        if not acc["added"]:
            acc["added"] = True
            events.append(self._emit("response.output_item.added", {
                "output_index": acc["output_index"],
                "item": {
                    "id": acc["call_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": acc["call_id"],
                    "name": acc["name"],
                    "arguments": "",
                },
            }))
            
        if fn.get("arguments"):
            acc["args"] += fn["arguments"]
            events.append(self._emit("response.function_call.delta", {
                "item_id": acc["call_id"],
                "output_index": acc["output_index"],
                "delta": fn["arguments"],
            }))
            
        return events

    def _process_dsml_content(self, content: str) -> List[str]:
        """Extract tool calls from MiniMax DSML markup in content."""
        events = []
        self.dsml_buffer += content
        
        while True:
            normalized = self.dsml_buffer.replace('\uff5c', '|').replace('<|DSML|', '|DSML|')
            
            invoke_pair = re.search(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|invoke>', normalized)
            if invoke_pair:
                tool_name = invoke_pair.group(1)
                inner = invoke_pair.group(2)
                pair_end = invoke_pair.end()
                
                # Create a synthetic tool call index for the map
                idx = 1000 + len(self.tool_acc)
                tid = f'call_dsml_{int(time.time() * 1000)}_{idx}'
                
                # Extract parameters
                params = {}
                for param_match in re.finditer(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/\|DSML\|parameter>', inner):
                    params[param_match.group(1)] = param_match.group(2)
                
                # Emit as tool delta
                events.extend(self._handle_tool_delta({"index": idx, "id": tid, "function": {"name": tool_name, "arguments": json.dumps(params)}}))
                
                self.dsml_buffer = self.dsml_buffer[pair_end:]
                continue
                
            if not self.in_dsml_mode:
                start_match = re.search(r'\|DSML\|tool_calls>', normalized)
                if start_match:
                    before = self.dsml_buffer[:start_match.start()]
                    if before.strip():
                        events.extend(self._handle_text_delta(before))
                    self.in_dsml_mode = True
                    self.dsml_buffer = self.dsml_buffer[start_match.end():]
                    continue
            
            if self.in_dsml_mode:
                end_match = re.search(r'</\|DSML\|tool_calls>', normalized)
                if end_match:
                    self.in_dsml_mode = False
                    after = self.dsml_buffer[end_match.end():]
                    self.dsml_buffer = ''
                    if after:
                        events.extend(self._process_dsml_content(after))
                    break
                break
                
            # No tags found, emit as text
            events.extend(self._handle_text_delta(self.dsml_buffer))
            self.dsml_buffer = ""
            break
            
        return events

    def force_done(self) -> List[str]:
        """Emit terminal events and close the stream."""
        if self.completed:
            return []
        self.completed = True
        
        events = []
        if not self.started:
            events.extend(self.start_events())
            
        # Finalize text
        if not self.text_started:
            events.extend(self._ensure_message_item())
            
        events.append(self._emit("response.output_text.done", {
            "item_id": self.msg_id, "output_index": self.msg_index,
            "content_index": 0, "text": self.full_text,
        }))
        events.append(self._emit("response.content_part.done", {
            "item_id": self.msg_id, "output_index": self.msg_index,
            "content_index": 0, "part": {"type": "output_text", "text": self.full_text, "annotations": []},
        }))
        events.append(self._emit("response.output_item.done", {
            "output_index": self.msg_index,
            "item": {
                "id": self.msg_id, "type": "message", "status": "completed",
                "role": "assistant", "content": [{"type": "output_text", "text": self.full_text, "annotations": []}],
            },
        }))
        
        # Finalize reasoning
        if self.reasoning_started:
            events.append(self._emit("response.reasoning_text.done", {
                "item_id": self.rsn_id, "output_index": self.rsn_index,
                "content_index": 0, "text": self.full_reasoning,
            }))
            events.append(self._emit("response.output_item.done", {
                "output_index": self.rsn_index,
                "item": {
                    "id": self.rsn_id, "type": "reasoning", "status": "completed",
                    "summary": "", "text": self.full_reasoning,
                },
            }))
            
        # Finalize tools
        for idx, acc in self.tool_acc.items():
            if acc["added"]:
                events.append(self._emit("response.output_item.done", {
                    "output_index": acc["output_index"],
                    "item": {
                        "id": acc["call_id"], "type": "function_call", "status": "completed",
                        "call_id": acc["call_id"], "name": acc["name"], "arguments": acc["args"],
                    },
                }))
                
        # Final response.completed
        outputs_by_index = {
            self.msg_index: {
                "id": self.msg_id, "type": "message", "status": "completed",
                "role": "assistant", "content": [{"type": "output_text", "text": self.full_text, "annotations": []}],
            }
        }
        if self.reasoning_started:
            outputs_by_index[self.rsn_index] = {
                "id": self.rsn_id, "type": "reasoning", "status": "completed",
                "summary": "", "text": self.full_reasoning,
            }
        for idx, acc in self.tool_acc.items():
            outputs_by_index[acc["output_index"]] = {
                "id": acc["call_id"], "type": "function_call", "status": "completed",
                "call_id": acc["call_id"], "name": acc["name"], "arguments": acc["args"],
            }
            
        final_outputs = [outputs_by_index[i] for i in sorted(outputs_by_index)]
        
        final_response = {
            "id": self.rid,
            "object": "response",
            "created_at": int(time.time()),
            "model": self.model,
            "status": "completed",
            "output": final_outputs,
            "usage": self.accum_usage,
        }
        events.append(self._emit("response.completed", {"response": final_response}))
        
        return events

    def get_assistant_message(self) -> dict:
        """Construct the OpenAI assistant message for history storage."""
        msg = {
            "role": "assistant",
            "content": self.full_text or (None if self.tool_acc else ""),
        }
        if self.tool_acc:
            msg["tool_calls"] = [
                {
                    "id": acc["call_id"],
                    "type": "function",
                    "function": {"name": acc["name"], "arguments": acc["args"]},
                }
                for acc in sorted(self.tool_acc.values(), key=lambda x: x["output_index"])
            ]
        return msg
