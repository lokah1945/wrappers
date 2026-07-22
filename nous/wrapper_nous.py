#!/usr/bin/env python3
"""
wrapper-nous/wrapper_nous.py  --  SINGLE PORT (9106) DUAL-FORMAT proxy.

Translates BOTH client wire formats into Nous Research "chat/completions"
(OpenAI-compatible, the only format Nous implements):

  1. OpenAI Responses API   (Codex v0.145)   -> POST /v1/responses
  2. Anthropic Messages API (Claude Code / SDK) -> POST /v1/messages
  3. OpenAI Chat Completions (pass-through)     -> POST /v1/chat/completions

Upstream: https://inference-api.nousresearch.com/v1/chat/completions
Auth:     FRESH Nous OAuth bearer token read live from Hermes auth.json
          per-request (auto handles expiry).

PATCH HISTORY (audit 2026-07-22):
  - BUG-002: _RESPONSE_STORE now guarded by STORE_LOCK (thread-safe).
  - BUG-001: responses_to_chat() now forwards tools + tool_choice to upstream.
  - BUG-003: _post_nous() retries on 429/5xx/timeout with exp-backoff+jitter.
  - BUG-006: real upstream streaming (Anthropic incremental via AnthropicStreamState;
             Responses text streaming; chat SSE byte-passthrough). Non-streaming
             fallback on any streaming error (no regression).
  - BUG-004/005 handled externally (systemd unit + nous_proxy.py quarantined).

Endpoints:
  POST /v1/responses           -> Responses -> chat -> Responses (SSE / JSON)
  POST /v1/messages            -> Anthropic -> chat -> Anthropic (SSE / JSON)
  POST /v1/chat/completions    -> pass-through to Nous (SSE / JSON)
  GET  /v1/models              -> proxy Nous models list (+ synthetic Anthropic IDs)
  GET  /healthz                -> liveness
"""
import json
import time
import random
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTH = "/root/.hermes/profiles/ilma/auth.json"
NOUS_BASE = "https://inference-api.nousresearch.com"
LISTEN = ("127.0.0.1", 9106)
DEFAULT_MODEL = "tencent/hy3:free"
REASONING_MODEL = "tencent/hy3:free"  # used when Anthropic thinking is enabled

# Claude Code only accepts a small allowlist of Anthropic model IDs at the
# CLIENT side. These IDs are "retired" on Anthropic but pass client validation,
# so we accept them and translate to a free Nous model here.
MODEL_ALIASES = {
    "claude-sonnet-4-20250514": "tencent/hy3:free",
    "claude-opus-4-20250514": "tencent/hy3:free",
    "claude-haiku-4-20250514": "tencent/hy3:free",
    "claude-sonnet-4-0": "tencent/hy3:free",
    "claude-opus-4-0": "tencent/hy3:free",
    "claude-haiku-4-0": "tencent/hy3:free",
    "sonnet": "tencent/hy3:free",
    "opus": "tencent/hy3:free",
    "haiku": "tencent/hy3:free",
    "fable": "tencent/hy3:free",
    "best": "tencent/hy3:free",
    "claude-sonnet-4-6": "tencent/hy3:free",
    "claude-opus-4-8": "tencent/hy3:free",
    "claude-haiku-4-5": "tencent/hy3:free",
}


def resolve_model(model):
    """Map a client model name to the actual Nous model to forward."""
    if not model:
        return DEFAULT_MODEL
    return MODEL_ALIASES.get(model, model)


# --------------------------------------------------------------------------
# Token
# --------------------------------------------------------------------------
def get_token():
    with open(AUTH) as f:
        d = json.load(f)
    n = d.get("providers", {}).get("nous", {})
    tok = n.get("access_token") or n.get("agent_key")
    if not tok:
        raise RuntimeError("no Nous token in auth.json")
    return tok


import datetime as _dt
def _debug_log(tag, data):
    try:
        with open("/tmp/wn_debug.log", "a") as f:
            f.write(f"[{_dt.datetime.now().isoformat()}] {tag}\n{data}\n{'='*60}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Upstream calls (with retry / streaming)
# --------------------------------------------------------------------------
def _post_nous(payload, token, max_retries=3):
    """Non-streaming POST to Nous chat/completions with retry on
    transient failures (429 / 5xx / network / timeout)."""
    url = NOUS_BASE + "/v1/chat/completions"
    _debug_log("SEND_TO_NOUS", json.dumps(payload)[:6000])
    try:
        with open("/tmp/wn_last_send.json", "w") as f:
            json.dump(payload, f)
    except Exception:
        pass
    last = (502, {"error": "unknown"})
    for attempt in range(max_retries + 1):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            r = urllib.request.urlopen(req, timeout=120)
            return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                cj = json.loads(e.read().decode(errors="replace") or "{}")
            except Exception:
                cj = {"error": e.read().decode(errors="replace") or str(e)}
            last = (e.code, cj)
            # Retryable: rate limit / upstream hiccup
            if e.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep((0.5 * (2 ** attempt)) + random.random() * 0.3)
                continue
            _debug_log("UPSTREAM", json.dumps({"model": payload.get("model"),
                        "status": last[0], "err": str(last[1])[:400]}))
            return last
        except Exception as e:  # URLError, timeout, etc.
            last = (502, {"error": str(e)})
            if attempt < max_retries:
                time.sleep((0.5 * (2 ** attempt)) + random.random() * 0.3)
                continue
            _debug_log("UPSTREAM", json.dumps({"model": payload.get("model"),
                        "status": last[0], "err": str(last[1])[:400]}))
            return last
    return last


def _open_nous_stream(payload, token):
    """Open a streaming POST to Nous chat/completions. Returns the urllib
    response object (SSE byte stream) on HTTP 200, raises HTTPError otherwise."""
    url = NOUS_BASE + "/v1/chat/completions"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream",
                 "Authorization": f"Bearer {token}"})
    r = urllib.request.urlopen(req, timeout=300)
    if r.status != 200:
        raise urllib.error.HTTPError(url, r.status, "", r.headers, r)
    return r


def _iter_nous_sse(r):
    """Yield parsed JSON chunks from an upstream SSE stream."""
    buf = b""
    for raw in r:
        buf += raw
        while b"\n\n" in buf:
            block, buf = buf.split(b"\n\n", 1)
            for line in block.split(b"\n"):
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload in (b"[DONE]", b"", b'"[DONE]"'):
                    return
                try:
                    yield json.loads(payload.decode())
                except Exception:
                    continue


def _get_nous_models(token):
    url = NOUS_BASE + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode(errors="replace") or "{}")


# ==========================================================================
# PART 1 -- OpenAI Responses API translator
# ==========================================================================
# In-memory conversation store for Responses API threading (thread-safe).
_RESPONSE_STORE = {}
_RESPONSE_TTL = 3600  # seconds
_STORE_LOCK = threading.Lock()


def _store_conversation(resp_id, messages):
    with _STORE_LOCK:
        if resp_id:
            _RESPONSE_STORE[resp_id] = (time.time(), messages)
        now = time.time()
        for k in list(_RESPONSE_STORE.keys()):
            if now - _RESPONSE_STORE[k][0] > _RESPONSE_TTL:
                _RESPONSE_STORE.pop(k, None)


def responses_to_chat(body):
    model = resolve_model(body.get("model"))
    raw = body.get("input")
    messages = []

    # Rebuild history from previous_response_id if present (stateless proxy)
    prev_id = body.get("previous_response_id")
    if prev_id and prev_id in _RESPONSE_STORE:
        _, prev_msgs = _RESPONSE_STORE[prev_id]
        messages.extend(prev_msgs)

    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                continue
            itype = item.get("type")
            if itype == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", "call"),
                    "content": str(item.get("output", "")),
                })
            elif itype == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id", "call"),
                        "type": "function",
                        "function": {
                            "name": item.get("name", "tool"),
                            "arguments": json.dumps(item.get("arguments", {})),
                        },
                    }],
                })
            else:
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "input_text")
                if role in ("user", "assistant", "system"):
                    messages.append({"role": role, "content": content})

    out = {"model": model, "messages": messages, "stream": False}
    if body.get("max_output_tokens"):
        out["max_tokens"] = max(int(body["max_output_tokens"]), 1024)
    else:
        out["max_tokens"] = 4096
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]

    # BUG-001 FIX: forward tools + tool_choice to upstream
    # Drop malformed tools (Codex sends deferred/discovery tools with name=null
    # which Nous chat/completions rejects -> 400 "Provider returned error").
    tools = body.get("tools")
    if tools:
        otools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn = t.get("function", t) if t.get("type") == "function" else t
            name = fn.get("name")
            if not name or not str(name).strip():
                continue  # skip unnamed/deferred tools
            otools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": normalize_schema(fn.get("parameters", {})),
                },
            })
        if otools:
            out["tools"] = otools
    if body.get("tool_choice"):
        out["tool_choice"] = body["tool_choice"]

    return out


def chat_to_responses(model, chat_resp):
    choices = chat_resp.get("choices") or []
    msg = choices[0].get("message", {}) if choices else {}
    text = msg.get("content") or msg.get("reasoning") or ""
    tool_calls = msg.get("tool_calls") or []
    output = []
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            output.append({
                "id": tc.get("id", "call-local"),
                "type": "function_call",
                "call_id": tc.get("id", "call-local"),
                "name": fn.get("name", "tool"),
                "arguments": fn.get("arguments", "{}"),
            })
    # Always include a text message item (even if empty) so the response has output
    output.append({
        "id": "msg-local",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    })
    return {
        "id": chat_resp.get("id", "resp-local"),
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": output,
        "status": "completed",
    }


def _build_messages_for_store(body, chat_resp):
    """Reconstruct the full message list (user/assistant/tool) for threading."""
    out = responses_to_chat(body).get("messages", [])
    choices = chat_resp.get("choices") or []
    msg = choices[0].get("message", {}) if choices else {}
    assistant_msg = {"role": "assistant", "content": msg.get("content", "")}
    if msg.get("tool_calls"):
        assistant_msg["tool_calls"] = [
            {
                "id": tc.get("id", "call"),
                "type": "function",
                "function": {
                    "name": tc.get("function", {}).get("name", "tool"),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                },
            } for tc in msg["tool_calls"]
        ]
    out.append(assistant_msg)
    return out


# ---- Responses SSE serialization + incremental streamer ----
def serialize_responses_event(ev):
    etype = ev.get("type")
    data = json.dumps(ev.get("data", {}))
    return f"event: {etype}\ndata: {data}\n\n"


class ResponsesStreamState:
    """Incremental Responses SSE emitter for a single (text) response."""
    def __init__(self, rid, item_id, model):
        self.rid = rid
        self.item_id = item_id
        self.model = model
        self.text = []
        self.ended = False

    def start_events(self):
        item = {"id": self.item_id, "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": ""}]}
        resp = {"id": self.rid, "object": "response",
                "created_at": int(time.time()), "model": self.model,
                "output": [item], "status": "in_progress"}
        return [
            {"type": "response.created", "data": resp},
            {"type": "response.in_progress",
             "data": {"type": "response", "id": self.rid, "status": "in_progress"}},
            {"type": "response.output_item.added",
             "data": {"type": "response.output_item.added", "output_index": 0, "item": item}},
            {"type": "response.content_part.added",
             "data": {"type": "response.content_part.added", "item_id": self.item_id,
                      "output_index": 0, "content_index": 0,
                      "part": {"type": "output_text", "text": ""}}},
        ]

    def delta_events(self, text):
        self.text.append(text)
        return [{"type": "response.output_text.delta",
                 "data": {"type": "response.output_text.delta", "item_id": self.item_id,
                          "output_index": 0, "content_index": 0, "delta": text}}]

    def done_events(self, usage=None):
        full = "".join(self.text)
        item = {"id": self.item_id, "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": full}]}
        self.ended = True
        return [
            {"type": "response.output_text.done",
             "data": {"type": "response.output_text.done", "item_id": self.item_id,
                      "output_index": 0, "content_index": 0, "text": full}},
            {"type": "response.content_part.done",
             "data": {"type": "response.content_part.done", "item_id": self.item_id,
                      "output_index": 0, "content_index": 0,
                      "part": {"type": "output_text", "text": full}}},
            {"type": "response.output_item.done",
             "data": {"type": "response.output_item.done", "output_index": 0, "item": item}},
            {"type": "response.completed",
             "data": {"id": self.rid, "object": "response",
                      "created_at": int(time.time()), "model": self.model,
                      "output": [item], "status": "completed",
                      "usage": usage or {}}},
        ]


def responses_sse_events(model, chat_resp):
    """Non-streaming chat response -> Responses SSE sequence (legacy helper)."""
    resp = chat_to_responses(model, chat_resp)
    rid = resp["id"]
    item = resp["output"][0]
    text = item["content"][0].get("text", "")
    return [
        {"type": "response.created", "data": resp},
        {"type": "response.in_progress", "data": {
            "type": "response", "id": rid, "status": "in_progress"}},
        {"type": "response.output_item.added", "data": {
            "type": "response.output_item.added",
            "output_index": 0, "item": item}},
        {"type": "response.content_part.added", "data": {
            "type": "response.content_part.added",
            "item_id": "msg-local", "output_index": 0, "content_index": 0,
            "part": {"type": "output_text", "text": ""}}},
        {"type": "response.output_text.delta", "data": {
            "type": "response.output_text.delta",
            "item_id": "msg-local", "output_index": 0,
            "content_index": 0, "delta": text}},
        {"type": "response.output_text.done", "data": {
            "type": "response.output_text.done",
            "item_id": "msg-local", "output_index": 0,
            "content_index": 0, "text": text}},
        {"type": "response.content_part.done", "data": {
            "type": "response.content_part.done",
            "item_id": "msg-local", "output_index": 0,
            "content_index": 0, "part": {"type": "output_text", "text": text}}},
        {"type": "response.output_item.done", "data": {
            "type": "response.output_item.done",
            "output_index": 0, "item": item}},
        {"type": "response.completed", "data": resp},
    ]


# ==========================================================================
# PART 2 -- Anthropic Messages API translator
# ==========================================================================
def normalize_schema(schema):
    """Clean JSON schema for OpenAI compatibility (per core.rs normalize_schema)."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if v is None:
            continue
        if k == "format" and v == "uri":
            continue
        if isinstance(v, dict):
            v = normalize_schema(v)
        elif isinstance(v, list):
            v = [normalize_schema(x) if isinstance(x, (dict, list)) else x for x in v]
        out[k] = v
    if out.get("type") == "object" and "required" not in out:
        out["required"] = []
    elif "required" in out and not isinstance(out["required"], list):
        out["required"] = []
    return out


def anthropic_to_openai(req):
    """Translate Anthropic /v1/messages request -> OpenAI chat/completions."""
    model = resolve_model(req.get("model"))
    thinking = req.get("thinking") or {}
    has_thinking = thinking.get("type") == "enabled"
    if has_thinking:
        model = REASONING_MODEL

    messages = []

    sys = req.get("system")
    if isinstance(sys, str) and sys:
        messages.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        for s in sys:
            if isinstance(s, dict):
                messages.append({"role": "system", "content": s.get("text", "")})
            elif isinstance(s, str):
                messages.append({"role": "system", "content": s})

    for m in req.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        text_parts = []
        reasoning_parts = []
        tool_calls = []
        for blk in content:
            btype = blk.get("type")
            if btype == "text":
                text_parts.append(blk.get("text", ""))
            elif btype == "thinking":
                reasoning_parts.append(blk.get("thinking", ""))
            elif btype == "image":
                src = blk.get("source", {})
                media = src.get("media_type", "image/png")
                data = src.get("data", "")
                text_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media};base64,{data}"}})
            elif btype == "tool_use":
                tool_calls.append({
                    "id": blk.get("id"),
                    "type": "function",
                    "function": {
                        "name": blk.get("name"),
                        "arguments": json.dumps(blk.get("input", {}))}})
            elif btype == "tool_result":
                tc = blk.get("tool_use_id")
                rc = blk.get("content")
                if isinstance(rc, str):
                    ttext = rc
                elif isinstance(rc, list):
                    ttext = "\n".join(
                        b.get("text", "") for b in rc
                        if isinstance(b, dict) and b.get("type") == "text")
                else:
                    ttext = ""
                messages.append({"role": "tool", "tool_call_id": tc, "content": ttext})
        if tool_calls:
            msg = {"role": role, "content": "\n".join(text_parts) or None,
                   "tool_calls": tool_calls}
            if reasoning_parts:
                msg["reasoning_content"] = "\n".join(reasoning_parts)
            messages.append(msg)
        elif role == "assistant" and reasoning_parts and not text_parts:
            messages.append({"role": "assistant",
                             "reasoning_content": "\n".join(reasoning_parts),
                             "content": None})
        else:
            if reasoning_parts:
                messages.append({"role": role,
                                 "reasoning_content": "\n".join(reasoning_parts),
                                 "content": "\n".join(text_parts)})
            else:
                messages.append({"role": role, "content": "\n".join(text_parts)})

    out = {"model": model, "messages": messages, "stream": False}
    mt = req.get("max_tokens")
    out["max_tokens"] = max(int(mt) if mt else 4096, 1024)
    if req.get("temperature") is not None:
        out["temperature"] = req["temperature"]
    if req.get("top_p") is not None:
        out["top_p"] = req["top_p"]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]
    tools = req.get("tools")
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": normalize_schema(t.get("input_schema", {}))}}
            for t in tools if t.get("name")]
    return out


def map_stop_reason(finish):
    if finish == "tool_calls":
        return "tool_use"
    if finish == "stop":
        return "end_turn"
    if finish == "length":
        return "max_tokens"
    return "end_turn"


def openai_to_anthropic(model, chat_resp):
    """Translate OpenAI chat/completions response -> Anthropic /v1/messages."""
    text = ""
    tool_uses = []
    choices = chat_resp.get("choices") or []
    if choices:
        msg = choices[0].get("message", {})
        text = msg.get("content") or ""
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if reasoning:
            text = (reasoning + "\n" + text).strip() if text else reasoning
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = json.loads(fn.get("arguments", "{}") or "{}")
            except Exception:
                inp = {}
            tool_uses.append({
                "type": "tool_use", "id": tc.get("id"),
                "name": fn.get("name"), "input": inp})
    content = []
    if text:
        content.append({"type": "text", "text": text})
    content.extend(tool_uses)
    if not content:
        content.append({"type": "text", "text": ""})
    usage = chat_resp.get("usage", {}) or {}
    fr = (choices[0].get("finish_reason") if choices else None)
    return {
        "id": chat_resp.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": map_stop_reason(fr),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ---- Anthropic streaming state machine (from stream.rs study) ----
class BlockState:
    IDLE = 0
    THINKING = 1
    TEXT = 2
    TOOLUSE = 3


class AnthropicStreamState:
    def __init__(self, fallback_model):
        self.message_id = None
        self.model = None
        self.fallback_model = fallback_model
        self.block = BlockState.IDLE
        self.next_index = 0
        self.message_started = False
        self.ended = False

    def start_events(self):
        return [{
            "type": "message_start",
            "data": {"type": "message_start", "message": {
                "id": self.message_id or "msg_proxy",
                "type": "message", "role": "assistant",
                "model": self.model or self.fallback_model,
                "usage": {"input_tokens": 0, "output_tokens": 0}}}}]

    def _close(self, events):
        if self.block != BlockState.IDLE:
            events.append({"type": "content_block_stop",
                           "index": self.next_index})
            self.next_index += 1
            self.block = BlockState.IDLE

    def translate_chunk(self, chunk):
        events = []
        if not chunk.get("choices"):
            return events
        ch = chunk["choices"][0]
        if self.message_id is None:
            self.message_id = chunk.get("id", "msg_proxy")
        if self.model is None:
            self.model = chunk.get("model", self.fallback_model)
        if not self.message_started:
            events.append({
                "type": "message_start",
                "data": {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model or self.fallback_model,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            })
            self.message_started = True
        delta = ch.get("delta", {}) or {}

        reasoning = delta.get("reasoning") or delta.get("reasoning_content")
        if reasoning:
            if self.block != BlockState.THINKING:
                self._close(events)
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.next_index,
                    "content_block": {"type": "thinking", "thinking": ""}}})
                self.block = BlockState.THINKING
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.next_index,
                "delta": {"type": "thinking_delta", "thinking": reasoning}}})

        if delta.get("content"):
            if self.block != BlockState.TEXT:
                self._close(events)
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.next_index,
                    "content_block": {"type": "text", "text": ""}}})
                self.block = BlockState.TEXT
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.next_index,
                "delta": {"type": "text_delta", "text": delta["content"]}}})

        for tc in delta.get("tool_calls") or []:
            if self.block != BlockState.TOOLUSE:
                self._close(events)
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.next_index,
                    "content_block": {"type": "tool_use",
                                      "id": tc.get("id"),
                                      "name": (tc.get("function") or {}).get("name", "")}}})
                self.block = BlockState.TOOLUSE
            pj = (tc.get("function") or {}).get("arguments", "")
            if pj:
                events.append({"type": "content_block_delta", "data": {
                    "type": "content_block_delta", "index": self.next_index,
                    "delta": {"type": "input_json_delta", "partial_json": pj}}})

        fr = ch.get("finish_reason")
        if fr:
            self._close(events)
            usage = chunk.get("usage") or {}
            events.append({"type": "message_delta", "data": {
                "type": "message_delta",
                "delta": {"stop_reason": map_stop_reason(fr), "stop_sequence": None},
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0)}}})
            events.append({"type": "message_stop", "data": {"type": "message_stop"}})
            self.ended = True
        return events

    def finish_events(self):
        events = []
        if self.block != BlockState.IDLE:
            events.append({"type": "content_block_stop", "index": self.next_index})
            self.next_index += 1
            self.block = BlockState.IDLE
        events.append({"type": "message_delta", "data": {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"input_tokens": 0, "output_tokens": 0}}})
        events.append({"type": "message_stop", "data": {"type": "message_stop"}})
        self.ended = True
        return events


def anthropic_sse_events(model, chat_resp):
    """Non-streaming chat response -> minimal Anthropic SSE sequence."""
    resp = openai_to_anthropic(model, chat_resp)
    events = [{"type": "message_start", "data": {
        "type": "message_start", "message": {
            "id": resp["id"], "type": "message", "role": "assistant",
            "model": resp["model"],
            "usage": {"input_tokens": resp["usage"]["input_tokens"],
                      "output_tokens": 0}}}}]
    idx = 0
    for blk in resp["content"]:
        btype = blk.get("type")
        if btype == "text":
            events.append({"type": "content_block_start", "data": {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "text", "text": ""}}})
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "text_delta", "text": blk.get("text", "")}}})
        elif btype == "tool_use":
            events.append({"type": "content_block_start", "data": {
                "type": "content_block_start", "index": idx,
                "content_block": {"type": "tool_use", "id": blk.get("id"),
                                  "name": blk.get("name", "")}}})
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": idx,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(blk.get("input", {}))}}})
        events.append({"type": "content_block_stop", "data": {
            "type": "content_block_stop", "index": idx}})
        idx += 1
    events.append({"type": "message_delta", "data": {
        "type": "message_delta",
        "delta": {"stop_reason": resp["stop_reason"], "stop_sequence": None},
        "usage": {"input_tokens": resp["usage"]["input_tokens"],
                  "output_tokens": resp["usage"]["output_tokens"]}}})
    events.append({"type": "message_stop", "data": {"type": "message_stop"}})
    return events


# ==========================================================================
# HTTP layer
# ==========================================================================
def serialize_anthropic_event(ev):
    etype = ev.get("type")
    data = json.dumps(ev.get("data", {}))
    return f"event: {etype}\ndata: {data}\n\n"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        payload = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _open_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def _write_sse(self, ev, serialize):
        self.wfile.write(serialize(ev).encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", "/health"):
            self._send(200, {"ok": True})
            return
        if self.path.startswith("/v1/models"):
            try:
                code, data = _get_nous_models(get_token())
                if isinstance(data, dict) and "data" in data:
                    synth = [
                        {"id": "claude-sonnet-4-6", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Claude Sonnet 4.6"},
                        {"id": "claude-opus-4-8", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Claude Opus 4.8"},
                        {"id": "claude-haiku-4-5", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Claude Haiku 4.5"},
                        {"id": "sonnet", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Sonnet"},
                        {"id": "opus", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Opus"},
                        {"id": "haiku", "object": "model", "created": 0,
                         "owned_by": "anthropic", "display_name": "Haiku"},
                    ]
                    existing = {m.get("id") for m in data["data"]}
                    for m in synth:
                        if m["id"] not in existing:
                            data["data"].append(m)
                self._send(code, data)
            except Exception as e:
                self._send(502, {"error": str(e)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            body = {}
        path = self.path.rstrip("/")
        _debug_log(f"REQ {path}", json.dumps(body)[:20000])
        try:
            with open("/tmp/wn_last_req.json", "w") as f:
                json.dump(body, f)
        except Exception:
            pass

        # ---- OpenAI Responses API (Codex) ----
        if path in ("/v1/responses", "/responses"):
            try:
                token = get_token()
                chat = responses_to_chat(body)
                model_out = chat["model"]
                if body.get("stream") and not body.get("tools"):
                    # Real upstream streaming (text turns only)
                    try:
                        r = _open_nous_stream({**chat, "stream": True}, token)
                        rid = "resp-" + str(int(time.time() * 1000))
                        rs = ResponsesStreamState(rid, "msg-local", model_out)
                        self._open_sse()
                        for ev in rs.start_events():
                            self._write_sse(ev, serialize_responses_event)
                        usage_final = {}
                        for chunk in _iter_nous_sse(r):
                            ch = (chunk.get("choices") or [{}])[0]
                            delta = ch.get("delta", {}) or {}
                            if delta.get("content"):
                                for ev in rs.delta_events(delta["content"]):
                                    self._write_sse(ev, serialize_responses_event)
                            if ch.get("finish_reason"):
                                usage_final = chunk.get("usage", {}) or {}
                        for ev in rs.done_events(usage=usage_final):
                            self._write_sse(ev, serialize_responses_event)
                    except Exception:
                        # Fallback to non-streaming (no regression)
                        code, cr = _post_nous(chat, token)
                        if code != 200:
                            self._send(code, cr)
                            return
                        resp = chat_to_responses(model_out, cr)
                        _store_conversation(resp["id"], _build_messages_for_store(body, cr))
                        self._send(200, resp)
                else:
                    code, cr = _post_nous(chat, token)
                    if code != 200:
                        self._send(code, cr)
                        return
                    resp = chat_to_responses(model_out, cr)
                    _store_conversation(resp["id"], _build_messages_for_store(body, cr))
                    self._send(200, resp)
            except Exception as e:
                self._send(502, {"error": str(e)})
            return

        # ---- Anthropic Messages API (Claude Code / SDK) ----
        if path in ("/v1/messages", "/messages"):
            try:
                token = get_token()
                chat = anthropic_to_openai(body)
                model_out = chat["model"]
                if body.get("stream"):
                    try:
                        r = _open_nous_stream({**chat, "stream": True}, token)
                        state = AnthropicStreamState(model_out)
                        state.message_id = "msg-" + str(int(time.time() * 1000))
                        state.model = model_out
                        state.message_started = True
                        self._open_sse()
                        for ev in state.start_events():
                            self._write_sse(ev, serialize_anthropic_event)
                        for chunk in _iter_nous_sse(r):
                            for ev in state.translate_chunk(chunk):
                                self._write_sse(ev, serialize_anthropic_event)
                        if not state.ended:
                            for ev in state.finish_events():
                                self._write_sse(ev, serialize_anthropic_event)
                    except Exception:
                        # Fallback to non-streaming
                        code, cr = _post_nous(chat, token)
                        if code != 200:
                            self._send(code, cr)
                            return
                        self._send(200, openai_to_anthropic(model_out, cr))
                else:
                    code, cr = _post_nous(chat, token)
                    if code != 200:
                        self._send(code, cr)
                        return
                    self._send(200, openai_to_anthropic(model_out, cr))
            except Exception as e:
                self._send(502, {"error": str(e)})
            return

        # ---- OpenAI Chat Completions pass-through ----
        if path in ("/v1/chat/completions", "/chat/completions"):
            try:
                token = get_token()
                fwd = {**body, "model": resolve_model(body.get("model", DEFAULT_MODEL))}
                if fwd.get("stream"):
                    try:
                        r = _open_nous_stream(fwd, token)
                        self._open_sse()
                        for raw_chunk in r:
                            self.wfile.write(raw_chunk)
                            self.wfile.flush()
                    except Exception as e:
                        self._send(502, {"error": str(e)})
                else:
                    code, data = _post_nous(fwd, token)
                    self._send(code, data)
            except Exception as e:
                self._send(502, {"error": str(e)})
            return

        self._send(404, {"error": "unsupported path " + self.path})


def main():
    srv = ThreadingHTTPServer(LISTEN, Handler)
    print(f"wrapper-nous unified proxy on http://{LISTEN[0]}:{LISTEN[1]} "
          f"(Responses + Anthropic + Chat)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
