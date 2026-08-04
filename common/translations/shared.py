#!/usr/bin/env python3
"""Shared translation utilities extracted from duplicated wrapper code.

These functions are identical (or near-identical) across nvidia-python,
nous, opencode, and blackbox wrappers. Centralizing them here means
bug fixes only need to be applied once.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any

try:
    from common.model.errors import looks_anti_bot_challenge
except Exception:  # pragma: no cover - fallback for standalone use
    def looks_anti_bot_challenge(payload: Any) -> bool:  # type: ignore[misc]
        return False

try:
    from common.sanitize_tokens import (
        filter_special_tokens as _filter_special_tokens,
        new_filter as _new_token_filter,
    )
except Exception:  # pragma: no cover - fallback for standalone use
    def _filter_special_tokens(t: str) -> str:  # type: ignore[misc]
        return t

    def _new_token_filter():  # type: ignore[misc]
        class _Passthrough:
            def feed(self, t):
                return t

            def flush(self):
                return ''
        return _Passthrough()


def finite_nonneg_int(value: Any) -> int:
    """Clamp a numeric to a finite non-negative int (token counters).

    B-27.1: upstream payloads may contain ``NaN``/``Infinity`` literals
    (Python's json.loads accepts them). ``int(NaN)`` raised ValueError and
    ``int(Inf)`` OverflowError inside response builders — turning an upstream
    SUCCESS into a client-visible crash (CONTRACT §3.3) — while a NaN added
    to a metrics counter poisoned it permanently (sticky), even into
    persisted snapshots. Non-finite, non-numeric, and negative values → 0.
    """
    try:
        v = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return v if v > 0 else 0


def responses_usage(input_tokens: int = 0, output_tokens: int = 0,
                    cached_tokens: int = 0, reasoning_tokens: int = 0) -> dict:
    """Full OpenAI Responses API usage object.

    P1-3 fix: the spec (and the strict openai SDK models) require the
    ``input_tokens_details`` and ``output_tokens_details`` nested objects;
    wrappers previously emitted a flat {input, output, total} triple that
    fails ``Response.model_validate_json``.
    """
    it = finite_nonneg_int(input_tokens)
    ot = finite_nonneg_int(output_tokens)
    return {
        "input_tokens": it,
        "input_tokens_details": {"cached_tokens": finite_nonneg_int(cached_tokens)},
        "output_tokens": ot,
        "output_tokens_details": {"reasoning_tokens": finite_nonneg_int(reasoning_tokens)},
        "total_tokens": it + ot,
    }


def tokens_from_chat_usage(u: Any) -> tuple:
    """(input, output, cached, reasoning) from an OpenAI chat usage dict.

    Returns finite non-negative ints (B-27.1: NaN/Inf/negatives → 0)."""
    u = u if isinstance(u, dict) else {}
    inp = u.get("prompt_tokens") or u.get("input_tokens") or 0
    out = u.get("completion_tokens") or u.get("output_tokens") or 0
    cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0
    reasoning = (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0
    return (finite_nonneg_int(inp), finite_nonneg_int(out),
            finite_nonneg_int(cached), finite_nonneg_int(reasoning))


def scrub_visible_text(text: str) -> str:
    """P0-4: one-shot special-token scrub for non-stream translations."""
    return _filter_special_tokens(text) if isinstance(text, str) else text


def responses_content_to_chat(content: Any) -> Any:
    """Convert an OpenAI Responses API input content array to chat content.

    P1-1 fix (audit 2026-08-03): ``input_image`` parts were silently DROPPED
    by the nous/opencode/blackbox/openrouter ``responses_to_chat`` converters
    (only text parts survived the join), so a vision request reached the
    model with the image missing and the model hallucinated an answer.
    nvidia-python already converted them; this helper is the single shared
    implementation (WRAPPER_CONTRACT §7).

    Returns a plain string when every part is text (legacy upstreams accept
    it everywhere), otherwise an OpenAI chat content-parts list with
    ``input_image`` → ``image_url`` conversion. Non-dict / unknown parts are
    skipped rather than crashing the turn.
    """
    if not isinstance(content, list):
        return content
    parts: list = []
    for p in content:
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if pt in ("input_text", "text", "output_text"):
            txt = p.get("text")
            parts.append({"type": "text", "text": txt if isinstance(txt, str) else ""})
        elif pt in ("input_image", "image_url", "image"):
            url = p.get("image_url") or p.get("url") or ""
            if isinstance(url, dict):
                url = url.get("url", "")
            if isinstance(url, str) and url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return " ".join(part["text"] for part in parts if part.get("text"))
    return parts


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
            # Incomplete DSML — MUST NOT leak partial markup (R5 audit: the
            # code appended the unterminated markup to clean_parts, doing the
            # exact opposite of this comment — a max_tokens-truncated tool
            # call arrived at the client as raw protocol text).
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
                "id": f"toolu_dsml_{int(time.time() * 1000)}_{hash(name) % 10000:04x}_{secrets.token_hex(3)}",
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
    # BUG-ECB2 fix: handle None payload gracefully
    if text_or_data is None:
        text_or_data = ""

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

    # BUG-ECB2 fix: provide meaningful default message when empty
    if not msg:
        msg = f"HTTP {status}" if status else "Unknown upstream error"

    # F1: never leak raw anti-bot HTML pages into SDK error messages — replace
    # with a concise, transient description (not an auth failure). Must be
    # detected BEFORE the status->type mapping below, which would otherwise
    # reclassify it as authentication_error and mislead SDKs into prompting
    # for new credentials.
    if looks_anti_bot_challenge(msg):
        msg = "Upstream anti-bot protection blocked the request (transient transport block, not an authentication failure)"
        etype = "api_error"
    elif status == 429:
        etype = "rate_limit_error"
    elif status in (401, 402, 403):
        etype = "authentication_error"
    elif status == 404:
        etype = "not_found_error"
    elif status >= 500:
        etype = "server_error"

    return {"error": {"message": str(msg)[:2000], "type": etype, "code": status}}


def parse_retry_after(resp_headers: dict, body: Any = None, default: int = 65) -> int:
    """Parse Retry-After from upstream response.

    Handles three sources (in priority order):
      1. HTTP `Retry-After` response header (int seconds OR RFC 1123 HTTP-date).
      2. JSON body keys: `retry_after`, `retry_after_seconds`, `retry-after`.
      3. Default fallback.

    This is the canonical implementation — used by nvidia-python, nous,
    opencode, blackbox, openrouter. Replaces 4 divergent _retry_after_seconds
    implementations across the wrappers.

    Args:
        resp_headers: dict-like with .get() (e.g. aiohttp.ClientResponse.headers
            or dict). Case-insensitive lookup attempted.
        body: parsed JSON body (dict) or None.
        default: fallback seconds if no source provides a value.

    Returns:
        int seconds to wait before retrying (minimum 1).
    """
    # 1. HTTP Retry-After header (int seconds or RFC 1123 date)
    if resp_headers:
        # Case-insensitive lookup
        ra = None
        if hasattr(resp_headers, 'get'):
            # Try common casings
            for k in ('Retry-After', 'retry-after', 'Retry-after', 'RETRY-AFTER'):
                ra = resp_headers.get(k)
                if ra:
                    break
        if ra:
            ra_str = str(ra).strip()
            # Try int seconds first
            try:
                return max(1, int(ra_str))
            except ValueError:
                pass
            # Try RFC 1123 HTTP-date
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(ra_str)
                if dt:
                    import datetime as _dt
                    now = _dt.datetime.now(_dt.timezone.utc)
                    delta = (dt - now).total_seconds()
                    if delta > 0:
                        return max(1, int(delta))
            except Exception:
                pass

    # 2. JSON body keys
    if isinstance(body, dict):
        err = body.get('error') if isinstance(body.get('error'), dict) else body
        for k in ('retry_after', 'retry_after_seconds', 'retry-after'):
            v = err.get(k) if isinstance(err, dict) else None
            if v is not None:
                try:
                    return max(1, int(float(v)))
                except (TypeError, ValueError):
                    pass

    # 3. Default
    return max(1, default)


def is_retriable_status(status: int) -> bool:
    """True if the HTTP status is retryable across keys (429, 5xx, 408, 409)."""
    return status == 429 or status >= 500 or status in (408, 409)


_MODEL_CAPACITY_MARKERS = (
    'no deployments available', 'selected model', 'cooldown_list',
    'invalid model name', 'model unavailable',
)


def looks_model_capacity_error(body: Any) -> bool:
    """True when the upstream error is about the MODEL, not the credential.

    B-21: promoted from four divergent per-wrapper copies. Cooling down a key
    for a model-capacity error wrongly removes a healthy credential from
    rotation for every other model it could still serve.
    """
    try:
        blob = json.dumps(body, ensure_ascii=False).lower() if isinstance(body, dict) else str(body).lower()
    except (TypeError, ValueError):
        blob = str(body).lower()
    except RecursionError:
        # B-24.1: a pathologically nested upstream error body (json.dumps and
        # str() both recurse) must classify as best-effort, not crash the
        # proxy error path under upstream distress (§3.3).
        blob = ''
    return any(marker in blob for marker in _MODEL_CAPACITY_MARKERS)


def should_cooldown_key(status: int, body: Any) -> bool:
    """Heuristic: should this response trigger a per-key cooldown?

    True for 429 (rate limit) and 401/403 (auth/quota) — these are
    per-credential failures that won't be fixed by retrying the same key.

    B-21 fix: the model-capacity carve-out (previously duplicated as a local
    `_should_cooldown_key` in nous and blackbox, which SHADOWED this shared
    import) is now part of the canonical implementation, so all five wrappers
    share one cooldown policy instead of silently diverging.
    """
    # A 429/404 caused by model capacity must NOT cool the credential.
    if status in (429, 404) and looks_model_capacity_error(body):
        return False
    # F1: anti-bot/Cloudflare transport blocks (403 with HTML body) are NOT
    # credential failures — cooldown would take the whole key pool offline.
    if status in (401, 402, 403) and looks_anti_bot_challenge(body):
        return False
    if status in (429, 401, 402, 403):
        return True
    if status in (408, 409) or status >= 500:
        return True
    # Some upstreams return 200 with an error envelope (rare but happens)
    if isinstance(body, dict):
        err = body.get('error') if isinstance(body.get('error'), dict) else body
        if isinstance(err, dict):
            msg = str(err.get('message', '')).lower()
            if 'rate limit' in msg or 'quota' in msg or 'too many requests' in msg:
                return True
    return False


# ── Transparent header forwarding ─────────────────────────────────────────
# Per project principle #1 (TRANSPARENT PROXY): wrappers must NOT drop client
# headers. The wrapper only swaps Authorization (to use a pool key) and strips
# hop-by-hop headers. Everything else is forwarded verbatim.

# Hop-by-hop headers (RFC 7230) — must NOT be forwarded by proxies.
HOP_BY_HOP_HEADERS = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
})

# Headers that the wrapper sets itself (must not be overwritten by client).
WRAPPER_OWNED_HEADERS = frozenset({
    'authorization', 'content-length', 'content-type', 'host',
    'accept-encoding',  # wrapper handles decompression
})

# Client headers that should ALWAYS be forwarded upstream (transparent proxy).
# This is a broad allowlist covering all standard OpenAI/Anthropic SDK headers
# plus common agent/client identity headers.
FORWARD_HEADER_ALLOWLIST = (
    # OpenAI / Anthropic SDK identity
    'user-agent', 'x-stainless-lang', 'x-stainless-package-version',
    'x-stainless-os', 'x-stainless-arch', 'x-stainless-runtime',
    'x-stainless-runtime-version', 'x-stainless-retry-count',
    # Anthropic-specific
    'anthropic-version', 'anthropic-beta', 'anthropic-dangerous-direct-browser-access',
    # OpenAI-specific
    'openai-beta', 'openai-organization', 'openai-project',
    # Request tracing
    'x-request-id', 'x-correlation-id', 'traceparent', 'tracestate',
    # Client identity (for logging/metrics upstream)
    'x-client-id', 'x-session-id',
    # Content negotiation
    'accept', 'accept-language',
    # Caching (some upstreams honor these)
    'if-none-match', 'if-modified-since',
)


def build_forward_headers(client_headers, extra: dict | None = None) -> dict:
    """Build the upstream header dict from client headers (transparent proxy).

    Forwards:
      - All headers in FORWARD_HEADER_ALLOWLIST (if present in client_headers).
      - Any additional headers from `extra` (wrapper-specific overrides).

    Strips:
      - Hop-by-hop headers (RFC 7230).
      - Wrapper-owned headers (authorization, content-length, content-type, host,
        accept-encoding) — these are set by the wrapper itself.

    Case-insensitive lookup on client_headers (works with Starlette/aiohttp
    headers objects that use case-insensitive .get()).

    Args:
        client_headers: dict-like with case-insensitive .get() (e.g.
            starlette.datastructures.Headers, dict, aiohttp.ClientResponse.headers).
        extra: optional dict of wrapper-specific headers to add/override.

    Returns:
        dict of headers to send upstream (all values sanitized to str).
    """
    out = {}
    if client_headers and hasattr(client_headers, 'get'):
        for h in FORWARD_HEADER_ALLOWLIST:
            v = client_headers.get(h)
            if v is not None:
                out[h] = str(v)
    if extra:
        for k, v in extra.items():
            if v is not None and k.lower() not in HOP_BY_HOP_HEADERS:
                out[k] = str(v)
    return out


def sanitize_header_value(value: str, max_len: int = 8192) -> str:
    """Sanitize a header value for safe forwarding.

    Strips CR/LF (CRLF injection prevention) and truncates to max_len.
    """
    if value is None:
        return ''
    s = str(value)
    # Strip CR/LF and other control chars (except tab, which is allowed in headers).
    s = ''.join(c for c in s if c == '\t' or (ord(c) >= 32 and ord(c) != 127))
    if len(s) > max_len:
        s = s[:max_len]
    return s


def anthropic_to_openai_response(a_resp: dict, request_model: str = "") -> dict:
    """Convert Anthropic Message response object -> OpenAI ChatCompletion response object.
    
    If input `a_resp` is already an OpenAI response (contains 'choices'), returns it as-is.
    """
    if not isinstance(a_resp, dict):
        return a_resp
    if "choices" in a_resp:
        return a_resp

    # R9: the fallback id must be unique per message — an ms timestamp alone
    # collides across concurrent turns (same class as the R7 store-key fix).
    msg_id = a_resp.get("id") or f"msg_{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    oai_id = f"chatcmpl-{msg_id}"
    model = a_resp.get("model") or request_model or ""
    role = a_resp.get("role", "assistant")
    content_blocks = a_resp.get("content", [])

    text_parts = []
    reasoning_parts = []
    tool_calls = []

    if isinstance(content_blocks, str):
        text_parts.append(content_blocks)
    elif isinstance(content_blocks, list):
        for b in content_blocks:
            if not isinstance(b, dict):
                continue
            b_type = b.get("type")
            if b_type == "text" and "text" in b:
                text_parts.append(str(b["text"]))
            elif b_type in ("thinking", "reasoning") and ("thinking" in b or "reasoning" in b):
                reasoning_parts.append(str(b.get("thinking") or b.get("reasoning") or ""))
            elif b_type == "tool_use":
                tc_id = b.get("id") or f"call_{len(tool_calls)}"
                tc_name = b.get("name") or ""
                tc_input = b.get("input", {})
                if not isinstance(tc_input, str):
                    try:
                        tc_args = json.dumps(tc_input)
                    except Exception:
                        tc_args = str(tc_input)
                else:
                    tc_args = tc_input
                tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tc_name,
                        "arguments": tc_args,
                    }
                })

    # P0-4: scrub tokenizer specials from visible channels.
    final_text = _filter_special_tokens("".join(text_parts))
    final_reasoning = _filter_special_tokens("".join(reasoning_parts))

    anthro_stop = a_resp.get("stop_reason")
    finish_map = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }
    finish_reason = finish_map.get(anthro_stop) or ("tool_calls" if tool_calls else "stop")

    msg_obj = {
        "role": role,
        "content": final_text or (None if tool_calls else ""),
    }
    if final_reasoning:
        msg_obj["reasoning_content"] = final_reasoning
    if tool_calls:
        msg_obj["tool_calls"] = tool_calls

    usage = a_resp.get("usage", {}) if isinstance(a_resp.get("usage"), dict) else {}
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0

    return {
        "id": oai_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": msg_obj,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
        }
    }


def new_response_id(prefix: str = "resp") -> str:
    """Mint a UNIQUE, time-ordered Responses-id: ``resp_<ms>-<12 hex>``.

    R7 concurrency finding (multi-agent storm): ms-timestamp ids collide
    under concurrent turns, and reusing an upstream ``chatcmpl-*`` id means
    every 429-retry or id-recycling gateway replays the SAME store key — two
    tenants then stored under one key and replayed each other's history
    (cross-agent data corruption). Store keys must be unique per turn; the
    millisecond prefix keeps ids roughly sortable for logs, the secrets
    suffix guarantees uniqueness. Use at every Responses-id minting site in
    every wrapper (CONTRACT §7 — do not fork this)."""
    return f"{prefix}_{int(time.time() * 1000)}-{secrets.token_hex(6)}"


def openai_to_anthropic_response(o_resp: dict, model: str = "", request_id: str = None) -> dict:
    """Convert OpenAI ChatCompletion response object -> Anthropic Message response object.

    If input `o_resp` is already an Anthropic response (type == 'message'), returns it as-is.
    """
    if not isinstance(o_resp, dict):
        return o_resp
    if o_resp.get("type") == "message" and "content" in o_resp:
        return o_resp

    choices = o_resp.get("choices") or [{}]
    choice = choices[0] if isinstance(choices, list) and choices else {}
    msg = choice.get("message") or {}

    content = []

    # Reasoning
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning and isinstance(reasoning, str):
        # P0-4 scrub + P1-2 signature field (required by strict SDK models).
        content.append({"type": "thinking",
                        "thinking": _filter_special_tokens(reasoning),
                        "signature": ""})

    # Text content
    raw_text = msg.get("content") or ""
    _dsml_tool_n = 0
    if raw_text:
        clean_text, dsml_tools = parse_dsml_from_text(raw_text)
        _dsml_tool_n = len(dsml_tools)
        # P0-4: scrub after DSML extraction so markup removal can't create
        # new token-looking fragments from legitimate text.
        clean_text = _filter_special_tokens(clean_text)
        if clean_text:
            content.append({"type": "text", "text": clean_text})
        for dt in dsml_tools:
            content.append(dt)
    elif not reasoning and not msg.get("tool_calls"):
        content.append({"type": "text", "text": ""})
    # P0-4 safety: if the special-token scrub emptied an otherwise contentless
    # message, keep a valid (empty) text block so the envelope stays parseable.
    if not content and not msg.get("tool_calls"):
        content.append({"type": "text", "text": ""})

    # Tool calls
    for tc in (msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {"raw": str(raw_args)}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{int(time.time()*1000)}-{secrets.token_hex(3)}",
            "name": fn.get("name") or "",
            "input": args,
        })

    finish_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
    }
    finish_reason = choice.get("finish_reason")
    if finish_reason is not None:
        # B-06 parity for non-streaming translation: an explicit upstream
        # finish_reason is authoritative even if the turn contains tool_use
        # blocks. Forcing tool_use whenever a tool was present masks
        # max_tokens/refusal/normal-stop and makes agents wait for tool_result.
        stop_reason = finish_map.get(finish_reason, "end_turn")
    else:
        # Only infer tool_use when the upstream omitted finish_reason entirely.
        stop_reason = "tool_use" if any(c.get("type") == "tool_use" for c in content) else "end_turn"
    # R5 audit: DSML markup is the ONLY tool-call signal MiniMax emits — the
    # turn still reports finish 'stop' (it does not know its markup is a
    # protocol). With recovered tool_use blocks in the message, end_turn
    # would make the agent close the turn and never execute the tool (the
    # "stops mid-process" class). Stream surfaces already upgrade; non-stream
    # must match (CONTRACT §8 parity). REAL tool_calls with finish 'stop'
    # still map to end_turn per B-06.
    if _dsml_tool_n and stop_reason == "end_turn":
        stop_reason = "tool_use"

    u = o_resp.get("usage") or {}
    prompt_tok = u.get("prompt_tokens") or u.get("input_tokens") or 0
    comp_tok = u.get("completion_tokens") or u.get("output_tokens") or 0
    cached_tok = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

    res_model = o_resp.get("model") or model or ""
    # R9: unique fallback id (ms alone collides across concurrent turns).
    res_id = request_id or o_resp.get("id") or f"msg_{int(time.time() * 1000)}-{secrets.token_hex(4)}"

    return {
        "id": str(res_id) if str(res_id).startswith("msg_") else f"msg_{res_id}",
        "type": "message",
        "role": "assistant",
        "model": res_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": prompt_tok,
            "output_tokens": comp_tok,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached_tok,
        }
    }


async def stream_anthropic_to_openai(anthropic_stream, model: str = ""):
    """Async generator: consume Anthropic SSE stream, yield OpenAI Chat SSE events."""
    msg_id = f"chatcmpl-{int(time.time() * 1000)}-{secrets.token_hex(4)}"  # R9: unique id
    # P0-4: stateful scrubbers for visible channels (cross-chunk fragments).
    _tok_text = _new_token_filter()
    _tok_reason = _new_token_filter()
    async def iter_lines():
        buffer = ""
        async for chunk in anthropic_stream:
            chunk_text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
            buffer += chunk_text
            lines = buffer.split("\n")
            buffer = lines.pop() if lines else ""
            for l in lines:
                yield l
        if buffer:
            yield buffer

    current_event = None
    async for line in iter_lines():
        line_str = line.strip()
        if not line_str:
            continue
        if line_str.startswith("event:"):
            current_event = line_str[6:].strip()
            continue
        if line_str.startswith("data:"):
            data_str = line_str[5:].strip()
            if not data_str:
                continue
            try:
                data = json.loads(data_str)
            except Exception:
                continue

            event_type = data.get("type") or current_event

            if event_type == "content_block_delta":
                delta = data.get("delta") or {}
                d_type = delta.get("type")
                if d_type == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        text = _tok_text.feed(text)
                        if text:
                            yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]})}\n\n"
                elif d_type == "thinking_delta":
                    thinking = delta.get("thinking", "")
                    if thinking:
                        thinking = _tok_reason.feed(thinking)
                        if thinking:
                            yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'reasoning_content': thinking}, 'finish_reason': None}]})}\n\n"
                elif d_type == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    idx = data.get("index", 0)
                    yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': idx, 'function': {'arguments': partial}}]}, 'finish_reason': None}]})}\n\n"

            elif event_type == "content_block_start":
                cb = data.get("content_block") or {}
                if cb.get("type") == "tool_use":
                    idx = data.get("index", 0)
                    tid = cb.get("id") or f"call_{idx}"
                    name = cb.get("name") or ""
                    yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': idx, 'id': tid, 'type': 'function', 'function': {'name': name, 'arguments': ''}}]}, 'finish_reason': None}]})}\n\n"

            elif event_type == "message_delta":
                delta = data.get("delta") or {}
                stop_reason = delta.get("stop_reason")
                finish_map = {"end_turn": "stop", "max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter"}
                finish = finish_map.get(stop_reason, "stop") if stop_reason else None
                # P0-4: release withheld tail text before the finish chunk.
                for _txt, _key in ((_tok_text.flush(), 'content'), (_tok_reason.flush(), 'reasoning_content')):
                    if _txt:
                        yield f"data: {json.dumps({'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model, 'choices': [{'index': 0, 'delta': {_key: _txt}, 'finish_reason': None}]})}\n\n"
                usage = data.get("usage") or {}
                oai_usage = {
                    "prompt_tokens": usage.get("input_tokens", 0),
                    "completion_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                } if usage else None
                out_chunk = {
                    'id': msg_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}],
                }
                if oai_usage:
                    out_chunk["usage"] = oai_usage
                yield f"data: {json.dumps(out_chunk)}\n\n"

            elif event_type == "message_stop":
                yield "data: [DONE]\n\n"


def _openai_content_to_anthropic(content: Any) -> Any:
    """Convert OpenAI chat content (str or parts list) -> Anthropic content.

    Handles text and image_url parts. Anthropic accepts base64 images only;
    data-URI image_urls are decoded into image blocks; http(s) URLs become a
    text placeholder (Anthropic has no URL-image support) so information is
    never silently dropped.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    blocks = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get('type')
        if ptype == 'text':
            txt = part.get('text', '')
            if txt:
                blocks.append({'type': 'text', 'text': txt})
        elif ptype == 'image_url':
            url = (part.get('image_url') or {}).get('url', '') if isinstance(part.get('image_url'), dict) else part.get('image_url', '')
            if isinstance(url, str) and url.startswith('data:'):
                # data:<media_type>;base64,<payload>
                try:
                    header, b64 = url.split(',', 1)
                    media_type = header[5:].split(';')[0] or 'image/png'
                    blocks.append({'type': 'image',
                                   'source': {'type': 'base64', 'media_type': media_type, 'data': b64}})
                except (ValueError, IndexError):
                    blocks.append({'type': 'text', 'text': url})
            elif url:
                blocks.append({'type': 'text', 'text': f'[image: {url}]'})
        else:
            # unknown part types preserved as inert text
            if part.get('text'):
                blocks.append({'type': 'text', 'text': part['text']})
    if not blocks:
        return None
    if len(blocks) == 1 and blocks[0]['type'] == 'text':
        return blocks[0]['text']
    return blocks


def openai_chat_to_anthropic_request(chat_body: dict) -> dict:
    """Convert OpenAI Chat Completions request -> Anthropic Messages request.

    This is the layer-2 (Anthropic upstream) request converter:
      - system/developer messages -> top-level `system`
      - assistant tool_calls -> tool_use blocks
      - role=tool messages -> user messages with tool_result blocks
      - image_url data URIs -> base64 image blocks (URLs -> text placeholder)
      - tools (function shape) -> Anthropic tools with input_schema
      - tool_choice auto/none/required/{function} -> {type: auto/none/any/tool}
      - stop (str|list) -> stop_sequences
      - max_tokens / temperature / top_p / stream forwarded
    """
    if not isinstance(chat_body, dict):
        return {'model': '', 'messages': []}

    model = chat_body.get('model', '')
    system_parts: list[str] = []
    msgs: list = []

    for m in chat_body.get('messages') or []:
        if not isinstance(m, dict):
            continue
        role = m.get('role', 'user')
        content = m.get('content')

        if role in ('system', 'developer'):
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                system_parts.append(''.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text'))
            continue

        if role == 'tool':
            tcid = m.get('tool_call_id') or ''
            rc = m.get('content', '')
            txt = rc if isinstance(rc, str) else json.dumps(rc, ensure_ascii=False) if rc is not None else ''
            msgs.append({
                'role': 'user',
                'content': [{'type': 'tool_result', 'tool_use_id': tcid, 'content': txt}],
            })
            continue

        if role == 'assistant':
            converted = _openai_content_to_anthropic(content)
            am: dict = {'role': 'assistant'}
            blocks = []
            if isinstance(converted, str):
                if converted:
                    blocks.append({'type': 'text', 'text': converted})
            elif isinstance(converted, list):
                blocks.extend(converted)
            for tc in m.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get('function') or {}
                raw_args = fn.get('arguments') or '{}'
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args)
                    except Exception:
                        parsed_args = {'raw': raw_args}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {'raw': str(raw_args)}
                blocks.append({
                    'type': 'tool_use',
                    'id': tc.get('id') or f'toolu_{int(time.time()*1000)}-{secrets.token_hex(3)}',
                    'name': fn.get('name') or '',
                    'input': parsed_args if isinstance(parsed_args, dict) else {'value': parsed_args},
                })
            if blocks:
                # Anthropic content must be a string or an ARRAY of blocks —
                # a bare dict is rejected by the Messages API. Always emit a
                # list when any block exists.
                am['content'] = blocks
            else:
                am['content'] = None
            msgs.append(am)
            continue

        # user
        converted = _openai_content_to_anthropic(content)
        if converted is None or converted == '':
            continue  # empty user shell (only tool_result blocks already emitted)
        msgs.append({'role': 'user', 'content': converted})

    out: dict = {'model': model, 'messages': msgs}
    if system_parts:
        out['system'] = '\n\n'.join(system_parts)
    if chat_body.get('stream') is not None:
        out['stream'] = bool(chat_body['stream'])
    # R12/B-12.3: coalesce the newer OpenAI alias — clients on recent SDKs
    # send `max_completion_tokens` instead of `max_tokens`; silently ignoring
    # it dropped the client's output cap entirely (unbounded generation).
    mt = chat_body.get('max_tokens')
    if mt is None:
        mt = chat_body.get('max_completion_tokens')
    if mt is not None:
        out['max_tokens'] = mt
    for k in ('temperature', 'top_p', 'top_k'):
        if chat_body.get(k) is not None:
            out[k] = chat_body[k]
    stop = chat_body.get('stop')
    if isinstance(stop, str):
        out['stop_sequences'] = [stop]
    elif isinstance(stop, list):
        out['stop_sequences'] = [str(s) for s in stop if s]
    # tools
    tools = []
    for t in chat_body.get('tools') or []:
        if not isinstance(t, dict):
            continue
        fn = t.get('function') if isinstance(t.get('function'), dict) else t
        name = fn.get('name') if isinstance(fn, dict) else None
        if not name:
            continue
        tools.append({
            'name': name,
            'description': fn.get('description', '') or '',
            'input_schema': fn.get('parameters') or {'type': 'object', 'properties': {}},
        })
    if tools:
        out['tools'] = tools
    # tool_choice
    tc = chat_body.get('tool_choice')
    if tc is not None:
        if isinstance(tc, str):
            if tc == 'none':
                out['tool_choice'] = {'type': 'none'}
            elif tc == 'required':
                out['tool_choice'] = {'type': 'any'}
            else:  # auto
                out['tool_choice'] = {'type': 'auto'}
        elif isinstance(tc, dict):
            t = tc.get('type')
            if t == 'function' and isinstance(tc.get('function'), dict):
                out['tool_choice'] = {'type': 'tool', 'name': tc['function'].get('name', '')}
            elif t == 'none':
                out['tool_choice'] = {'type': 'none'}
            elif t == 'required':
                out['tool_choice'] = {'type': 'any'}
            else:
                out['tool_choice'] = {'type': 'auto'}
    return out
