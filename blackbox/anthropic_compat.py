"""
anthropic_compat.py — Anthropic Messages API ⇄ OpenAI Chat Completions translation
for wrapper-blackbox. Lets Anthropic-SDK clients hit POST /v1/messages while the
wrapper proxies to Blackbox AI's OpenAI-compatible /v1/chat/completions.

Model handling is PASS-THROUGH: the client must send a Blackbox model id in the
`model` field (e.g. "blackboxai/openai/gpt-5.5"). No alias remapping.

Three translators:
  - anthropic_to_openai(body)            request  A→O
  - openai_to_anthropic(resp, model)     response O→A  (non-streaming)
  - stream_openai_to_anthropic(resp, …)  response O→A  (SSE, async generator)

Adapted from wrapper-nvidia anthropic_compat.py for Blackbox AI.
"""
import json

_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: "end_turn",
}


def anthropic_error(etype: str, message: str) -> dict:
    return {"type": "error", "error": {"type": etype, "message": message}}


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


# ── Request: Anthropic → OpenAI ──────────────────────────────────────────

def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return ""


def anthropic_to_openai(a: dict) -> dict:
    oai = {"model": a.get("model", "") or ""}
    msgs = []

    # system → leading system message
    sys = a.get("system")
    sys_text = sys if isinstance(sys, str) else _flatten_text(sys)
    if sys_text:
        msgs.append({"role": "system", "content": sys_text})

    for m in a.get("messages", []):
        role = m.get("role")
        content = m.get("content")

        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue

        parts, tool_calls, tool_results = [], [], []
        for blk in content or []:
            t = blk.get("type")
            if t == "text":
                parts.append({"type": "text", "text": blk.get("text", "")})
            elif t == "image":
                src = blk.get("source", {}) or {}
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
                else:
                    url = src.get("url", "")
                parts.append({"type": "image_url", "image_url": {"url": url}})
            elif t == "tool_use":
                tool_calls.append({
                    "id": blk.get("id"),
                    "type": "function",
                    "function": {"name": blk.get("name"),
                                 "arguments": json.dumps(blk.get("input", {}) or {})},
                })
            elif t == "tool_result":
                c = blk.get("content", "")
                c = _flatten_text(c) if isinstance(c, list) else c
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": blk.get("tool_use_id"),
                    "content": c if isinstance(c, str) else json.dumps(c),
                })

        if role == "user":
            # OpenAI wants tool results as their own role:"tool" messages first
            msgs.extend(tool_results)
            if parts:
                if all(p["type"] == "text" for p in parts):
                    msgs.append({"role": "user",
                                 "content": "".join(p["text"] for p in parts)})
                else:
                    msgs.append({"role": "user", "content": parts})
        elif role == "assistant":
            am = {"role": "assistant"}
            txt = "".join(p["text"] for p in parts if p["type"] == "text")
            # OpenAI allows content=null only when tool_calls are present;
            # otherwise send "" to avoid a 400 on empty assistant turns.
            am["content"] = txt if txt else (None if tool_calls else "")
            if tool_calls:
                am["tool_calls"] = tool_calls
            msgs.append(am)

    oai["messages"] = msgs

    if a.get("max_tokens") is not None:
        oai["max_tokens"] = a["max_tokens"]
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("top_k", "top_k"), ("stop_sequences", "stop")):
        if a.get(src) is not None:
            oai[dst] = a[src]

    if a.get("tools"):
        oai["tools"] = [{"type": "function", "function": {
            "name": t.get("name"),
            "description": t.get("description"),
            "parameters": t.get("input_schema", {}) or {},
        }} for t in a["tools"]]
        if a.get("tool_choice"):
            tc = a["tool_choice"]
            if tc == "auto" or tc == "any":
                oai["tool_choice"] = tc
            elif isinstance(tc, dict) and tc.get("type") == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}

    return oai


# ── Token estimation (for /v1/messages/count_tokens) ─────────────────────

def estimate_input_tokens(a: dict) -> int:
    """Rough char/4 estimate for Anthropic request body."""
    chars = 0
    sys = a.get("system")
    if sys:
        chars += len(sys) if isinstance(sys, str) else sum(len(b.get("text", "")) for b in sys if isinstance(b, dict))
    for m in a.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    if b.get("type") == "text":
                        chars += len(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        chars += len(json.dumps(b.get("input", {}) or {}))
    for t in a.get("tools", []):
        chars += len(json.dumps(t.get("input_schema", {}) or {}))
    return max(1, -(-chars // 4))  # ceil(chars/4)


# ── Response: OpenAI → Anthropic (non-streaming) ─────────────────────────

def openai_to_anthropic(o: dict, model: str) -> dict:
    choice = (o.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id"),
                        "name": fn.get("name"), "input": args})

    u = o.get("usage") or {}
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    usage = {"input_tokens": u.get("prompt_tokens", 0) or 0,
             "output_tokens": u.get("completion_tokens", 0) or 0}
    if cached:
        usage["cache_read_input_tokens"] = cached

    return {
        "id": o.get("id") or "msg_wrapper",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _FINISH_TO_STOP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": usage,
    }


# ── Response: OpenAI SSE → Anthropic SSE (streaming) ─────────────────────

async def stream_openai_to_anthropic(resp, model: str, capture: dict):
    """Async generator: consume Blackbox OpenAI SSE, emit Anthropic event stream.
    Captures final usage/stop into `capture` for the caller's metrics."""
    msg_id = "msg_wrapper"
    text_index = None
    tool_map = {}          # openai tool index -> anthropic block index
    next_index = 0
    open_idx = None
    final_stop = "end_turn"
    usage = {}

    yield _sse("message_start", {
        "type": "message_start",
        "message": {"id": msg_id, "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0}},
    })
    yield _sse("ping", {"type": "ping"})

    try:
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}

            # text delta
            if delta.get("content"):
                if text_index is None:
                    if open_idx is not None:
                        yield _sse("content_block_stop",
                                   {"type": "content_block_stop", "index": open_idx})
                    text_index = next_index
                    next_index += 1
                    open_idx = text_index
                    yield _sse("content_block_start", {
                        "type": "content_block_start", "index": text_index,
                        "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {
                    "type": "content_block_delta", "index": text_index,
                    "delta": {"type": "text_delta", "text": delta["content"]}})

            # tool-call deltas
            for tc in delta.get("tool_calls") or []:
                oi = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oi not in tool_map:
                    if open_idx is not None:
                        yield _sse("content_block_stop",
                                   {"type": "content_block_stop", "index": open_idx})
                    ai = next_index
                    next_index += 1
                    tool_map[oi] = ai
                    open_idx = ai
                    yield _sse("content_block_start", {
                        "type": "content_block_start", "index": ai,
                        "content_block": {"type": "tool_use",
                                          "id": tc.get("id") or f"toolu_{ai}",
                                          "name": fn.get("name") or "",
                                          "input": {}}})
                ai = tool_map[oi]
                if fn.get("name"):
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta", "index": ai,
                        "delta": {"type": "tool_use_delta", "name": fn["name"]}})
                if fn.get("arguments"):
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta", "index": ai,
                        "delta": {"type": "tool_use_delta", "input": fn["arguments"]}})

            # finish
            if ch.get("finish_reason"):
                final_stop = _FINISH_TO_STOP.get(ch["finish_reason"], "end_turn")
                if open_idx is not None:
                    yield _sse("content_block_stop",
                               {"type": "content_block_stop", "index": open_idx})
                yield _sse("message_delta", {
                    "type": "message_delta", "delta": {"stop_reason": final_stop}})
                yield _sse("message_stop", {"type": "message_stop"})

    finally:
        capture["usage"] = usage
        capture["stop_reason"] = final_stop