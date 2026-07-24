#!/usr/bin/env python3
"""
wrapper-opencode — FastAPI proxy for OpenCode (similar architecture to wrapper-nvidia).
OpenAI + Anthropic compatible + Responses API.

Production features:
- Multi-key rotation + pacing + load shedding (INFLIGHT_SOFT_CAP=100)
- Full streaming with anti-silence + heartbeat
- OpenAI Chat + Responses + Anthropic Messages
- .env hot reload
- Rich metrics
"""

import os
import json
import re
import time
import threading
import asyncio
import logging
from typing import Set
from pathlib import Path
from contextlib import asynccontextmanager

# Shared persistent catalog/state layer; bootstrap repo root for systemd launches.
try:
    from common.model_state import ModelStateStore, classify_upstream_error
    from common.model import LocalModelRegistry
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import ModelStateStore, classify_upstream_error
    from common.model import LocalModelRegistry

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from .key_pool import KeyPool
from .metrics import Metrics

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
# Fallback: also try cwd-relative .env (for direct uvicorn launches)
if not os.environ.get('OPENCODE_BASE_URL'):
    load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/opencode/opencode.log')
try:
    os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
    _log_file_handler = logging.FileHandler(LOG_FILE)
except Exception:
    LOG_FILE = '/tmp/wrapper-opencode.log'
    _log_file_handler = logging.FileHandler(LOG_FILE)
logger = logging.getLogger('wrapper-opencode')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [opencode] %(message)s',
    handlers=[
        _log_file_handler,
        logging.StreamHandler(),
    ],
)

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9103'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
OPENCODE_BASE = os.environ.get('OPENCODE_BASE_URL', 'https://opencode.ai/zen/v1').rstrip('/')
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).resolve().parents[1] / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '21600'))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get('MODEL_CATALOG_REFRESH_SEC', '86400'))
MODEL_STORE = ModelStateStore('opencode', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('opencode')
_MODEL_REFRESH_TASK = None
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get('MAX_CONNECTIONS_PER_HOST', '100'))
CONNECT_TIMEOUT_SEC = int(os.environ.get('CONNECT_TIMEOUT_SEC', '30'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT_SEC', '600'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '900'))
# No DEFAULT_MODEL - all model selection is transparent (client chooses)
VERSION = '1.0.5-anthropic-tools'

def free_only_enabled() -> bool:
    """FREE_ONLY=yes|true|1 → only models with 'free' in the name."""
    v = (os.environ.get('FREE_ONLY') or 'no').strip().lower()
    return v in ('yes', 'true', '1', 'on', 'y')

def is_free_model(model_id: str) -> bool:
    """True if model id contains 'free', or is listed in FREE_MODEL_ALLOWLIST.

    OpenCode Zen free catalog mostly uses *-free ids; `big-pickle` is free but
    has no 'free' substring — add it via FREE_MODEL_ALLOWLIST=big-pickle if needed.
    """
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    if mid.startswith('opencode/'):
        mid = mid.split('/', 1)[1]
    if 'free' in mid:
        return True
    allow = (os.environ.get('FREE_MODEL_ALLOWLIST') or '').strip()
    if not allow:
        return False
    extras = {x.strip().lower() for x in allow.split(',') if x.strip()}
    bare = mid.split('/')[-1] if '/' in mid else mid
    return mid in extras or bare in extras

def model_allowed(model_id: str) -> bool:
    if not free_only_enabled():
        return True
    if not model_id:
        return False
    raw = str(model_id).strip()
    if raw.lower().startswith('opencode/'):
        raw = raw.split('/', 1)[1]
    if is_alias_name(raw):
        tgt = get_dynamic_alias_target()
        return bool(tgt) and is_free_model(tgt)
    return is_free_model(raw)


def free_only_error(model_id: str) -> dict:
    return {
        'error': {
            'type': 'invalid_request_error',
            'message': (
                f'Model "{model_id}" is blocked by FREE_ONLY=yes. '
                'Only model ids containing "free" are allowed '
                '(plus any ids in FREE_MODEL_ALLOWLIST). '
                'Set FREE_ONLY=no to allow paid models. '
                'This wrapper does not substitute models — send a free model id from the client.'
            ),
            'code': 'free_only_restricted',
            'param': 'model',
        }
    }

def free_only_anthropic_error(model_id: str) -> dict:
    return {
        'type': 'error',
        'error': {
            'type': 'invalid_request_error',
            'message': free_only_error(model_id)['error']['message'],
        },
    }

# Dynamic aliases: NO hardcoded model targets.
# Calling minimaxai/minimax-m3 or z-ai/glm-5.2 binds sonnet/haiku/opus/claude-* to that id.
_ALIAS_NAME_SET = {
    'sonnet', 'opus', 'haiku',
    'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-haiku-4-5',
    'claude-sonnet-4-20250514', 'claude-opus-4-20250514', 'claude-haiku-4-20250514',
    'claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4',
    'claude-sonnet', 'claude-opus', 'claude-haiku',
    'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022',
}
_dynamic_alias_target = ''
_dynamic_alias_lock = threading.Lock()
_known_models: Set[str] = set()

def is_alias_name(model_id: str) -> bool:
    if not model_id:
        return False
    return str(model_id).lower().strip() in _ALIAS_NAME_SET

def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ''

def set_dynamic_alias_target(model_id: str, force: bool = False) -> None:
    global _dynamic_alias_target
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if mid.lower().startswith('opencode/'):
        mid = mid.split('/', 1)[1]
    if not mid:
        return
    if not force and mid not in _known_models:
        logger.debug(f'[alias] ignoring unknown model {mid!r} — not in known model catalog')
        return
    with _dynamic_alias_lock:
        if _dynamic_alias_target != mid:
            logger.info(f'[alias] dynamic target bound → {mid}')
        _dynamic_alias_target = mid

BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()
ANTI_SILENCE = int(os.environ.get('ANTI_SILENCE_TIMEOUT_MS', '960000'))
INFLIGHT_SOFT_CAP = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))

pool = KeyPool()
metrics = Metrics()

_session = None

async def get_session():
    """Reuse one aiohttp session (fix per-request ClientSession leak)."""
    global _session
    import aiohttp
    need_new = _session is None or _session.closed
    if not need_new:
        try:
            loop = asyncio.get_running_loop()
            sess_loop = getattr(_session, '_loop', None)
            if sess_loop is not None and (sess_loop.is_closed() or sess_loop is not loop):
                need_new = True
        except Exception:
            need_new = True
    if need_new:
        if _session is not None and not _session.closed:
            try:
                await _session.close()
            except Exception:
                pass
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=max(REQUEST_TIMEOUT_SEC, STREAM_REQUEST_TIMEOUT_SEC), sock_connect=CONNECT_TIMEOUT_SEC),
            connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS_PER_HOST, ttl_dns_cache=300, enable_cleanup_closed=True),
        )
    return _session

def _zen_family(model: str) -> str:
    """Map model id → Zen endpoint family per https://opencode.ai/docs/zen/"""
    m = (model or '').lower().strip()
    if m.startswith('opencode/'):
        m = m[len('opencode/'):]
    # Aliases route by the bound concrete target, never by a hardcoded map
    if is_alias_name(m):
        tgt = get_dynamic_alias_target()
        if not tgt:
            return 'chat'
        m = tgt.lower().strip()
    if m.startswith('gpt-') or m in ('gpt-5',):
        return 'responses'
    if m.startswith('claude-'):
        return 'messages'
    if m.startswith('gemini-'):
        return 'google'
    if m.startswith('qwen3.') or m.startswith('qwen3-') or m.startswith('qwen3'):
        return 'messages'
    # Free models (OpenAI-compatible per Zen docs) → chat/completions
    if is_free_model(m):
        return 'chat'
    # Zen OpenAI-compatible models (Grok, DeepSeek, MiniMax, GLM, Kimi, etc.)
    # → chat/completions (the Responses API translate branch handles them).
    return 'chat'


def _normalize_model(model: str) -> str:
    """Transparent pass-through + dynamic aliases (no hardcoded targets).

    Concrete id (minimaxai/minimax-m3, z-ai/glm-5.2, ...) passes through and
    binds all aliases. Alias names resolve to the current bound target only.
    """
    if model is None:
        return ""
    m = str(model).strip()
    if not m:
        return ""
    if m.lower().startswith('opencode/'):
        m = m.split('/', 1)[1]
    if is_alias_name(m):
        tgt = get_dynamic_alias_target()
        return tgt if tgt else m
    # Concrete requests never mutate process-wide alias state.
    return m


def _normalize_upstream_error(status: int, text_or_data) -> dict:
    """Single OpenAI-shaped error; unwrap nested JSON string messages."""
    msg = text_or_data
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
            return _normalize_upstream_error(status, parsed)
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
    elif status in (401, 403):
        etype = "authentication_error"
    elif status == 402 or (isinstance(etype, str) and "credit" in etype.lower()):
        etype = "authentication_error"
    elif status == 404:
        etype = "not_found_error"
    elif status >= 500:
        etype = "server_error"
    return {"error": {"message": str(msg)[:2000], "type": etype, "code": status}}

async def proxy_request(method: str, url: str, json_body: dict = None, headers: dict = None, is_stream: bool = False):
    import aiohttp
    sess = await get_session()
    headers = headers or {}
    try:
        if is_stream:
            # Caller owns release — do NOT async-with the response
            resp = await sess.request(
                method, url, json=json_body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=STREAM_REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC),
            )
            if resp.status >= 400:
                text = await resp.text()
                resp.release()
                try:
                    data = json.loads(text)
                except Exception:
                    data = text
                return resp.status, _normalize_upstream_error(resp.status, data)
            return 200, resp
        async with sess.request(
            method, url, json=json_body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC),
        ) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = text
            if resp.status >= 400:
                return resp.status, _normalize_upstream_error(resp.status, data)
            if not isinstance(data, dict):
                data = {"error": {"message": str(data)[:2000], "type": "api_error"}}
            return resp.status, data
    except Exception as e:
        return 502, {"error": {"message": str(e), "type": "api_error"}}




def _retry_after_seconds(data, default=65) -> int:
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else data
        for k in ("retry_after", "retry_after_seconds", "retry-after"):
            v = err.get(k) if isinstance(err, dict) else None
            if v is not None:
                try:
                    return max(1, int(float(v)))
                except (TypeError, ValueError):
                    pass
    return default


def _is_retriable_upstream_status(status: int, data=None) -> bool:
    if status in (401, 402, 403, 408, 409, 429):
        return True
    if status >= 500:
        return True
    return False


def _looks_model_capacity_error(data) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    return any(x in blob for x in ('no deployments available', 'selected model', 'cooldown_list', 'invalid model name', 'model unavailable'))


def _should_cooldown_key(status: int, data) -> bool:
    if status == 429 and _looks_model_capacity_error(data):
        return False
    if status == 404 and _looks_model_capacity_error(data):
        return False
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


async def proxy_request_with_pool(method: str, url: str, json_body: dict, request: Request, is_stream: bool = False):
    """Call upstream with all available keys before surfacing an error.

    A single rate-limited/bad key is cooled down and the request is retried with
    the next key. Only after every available key fails do we return an error to
    the client/agent.
    """
    attempts = max(1, pool.total_keys)
    last_status = 503
    last_data = {"error": {"message": "No capacity", "type": "server_error"}}
    tried = 0
    for _ in range(attempts):
        key_result = await pool.acquire()
        if not key_result:
            break
        key = key_result["key"]
        headers = _auth_headers(key.api_key, request)
        if url.endswith('/messages') and not headers.get('anthropic-version'):
            headers['anthropic-version'] = '2023-06-01'
        status, data = await proxy_request(method, url, json_body, headers, is_stream=is_stream)
        model_id = json_body.get('model', '') if isinstance(json_body, dict) else ''
        if model_id:
            try:
                from common.model_state import credential_fingerprint
                if status == 200:
                    MODEL_STORE.record_status(model_id, credential_fingerprint(key.api_key), 'available', status, 'OK', endpoint=url)
                else:
                    MODEL_STORE.record_error(model_id, key.api_key, status, data, endpoint=url)
            except Exception as e:
                logger.warning(f'[model-state] OpenCode result record failed: {e}')
        if status == 200:
            if is_stream:
                return status, data, key
            pool.release(key)
            return status, data, None
        tried += 1
        last_status, last_data = status, data
        classification = classify_upstream_error(status, data)
        if _is_retriable_upstream_status(status, data) and classification['retry_same_model']:
            if _should_cooldown_key(status, data):
                pool.mark_failure(key, status, _retry_after_seconds(data), 'upstream')
            pool.release(key)
            continue
        pool.release(key)
        return status, data, None
    if tried >= max(1, pool.total_keys) and isinstance(last_data, dict) and last_data.get("error"):
        last_data = {"error": {**last_data["error"], "message": f"All configured OpenCode keys failed or are rate-limited. Last error: {last_data['error'].get('message', '')}"[:2000]}}
    return last_status, last_data, None

def _ensure_chat_message(data: dict) -> dict:
    """Normalize chat completion message for strict OpenAI clients."""
    if not isinstance(data, dict):
        return data
    try:
        choices = data.get("choices") or []
        if not choices:
            return data
        ch0 = choices[0] or {}
        msg = ch0.get("message") or {}
        if msg.get("content") is None:
            msg["content"] = ""
        ch0["message"] = msg
        choices[0] = ch0
        data["choices"] = choices
        if not data.get('usage'):
            data['usage'] = {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
            }
    except Exception:
        pass
    return data

def _jr(status: int, content: dict):
    """JSONResponse with correct kw-only args (Starlette)."""
    return JSONResponse(status_code=status, content=content)

def _auth_headers(api_key: str, request: Request = None) -> dict:
    h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept-Encoding": "identity"}
    if request is not None:
        for k in ("anthropic-beta", "anthropic-version", "openai-beta", "x-api-key"):
            v = request.headers.get(k)
            if v:
                h[k] = v
    return h

# --- minimal Anthropic <-> OpenAI helpers (surgical, local) ---
def _strip_cache(obj):
    if isinstance(obj, dict):
        obj.pop('cache_control', None)
        for v in list(obj.values()):
            _strip_cache(v)
    elif isinstance(obj, list):
        for x in obj:
            _strip_cache(x)

def anthropic_to_openai(body: dict) -> dict:
    _strip_cache(body)
    model = _normalize_model(body.get('model') or '')
    msgs = []
    sys = body.get('system')
    if isinstance(sys, str) and sys:
        msgs.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        texts = [s.get('text', str(s)) if isinstance(s, dict) else str(s) for s in sys]
        if texts:
            msgs.append({"role": "system", "content": "\n".join(texts)})
    for m in body.get('messages') or []:
        role, c = m.get('role'), m.get('content')
        if isinstance(c, str):
            msgs.append({"role": role, "content": c}); continue
        if not isinstance(c, list):
            msgs.append({"role": role, "content": c}); continue
        parts, tools = [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get('type')
            if t == 'text':
                parts.append({"type": "text", "text": b.get('text', '')})
            elif t == 'tool_use':
                tools.append({"id": b.get('id'), "type": "function",
                              "function": {"name": b.get('name'), "arguments": json.dumps(b.get('input') or {})}})
            elif t == 'tool_result':
                rc = b.get('content')
                txt = rc if isinstance(rc, str) else "\n".join(x.get('text','') for x in (rc or []) if isinstance(x, dict))
                msgs.append({"role": "tool", "tool_call_id": b.get('tool_use_id'), "content": txt})
            elif t == 'thinking':
                # Preserve as reasoning_content — never dump raw into text
                pass
        # Collect thinking blocks
        thinking_parts = []
        for b in (c if isinstance(c, list) else []):
            if isinstance(b, dict) and b.get('type') == 'thinking':
                thinking_parts.append(b.get('thinking') or '')
        final = parts if len(parts) > 1 else (parts[0]['text'] if parts else ('' if tools else None))
        if role == 'user' and not parts and not tools:
            continue  # only tool_results already emitted
        if role == 'assistant' and not parts and not tools and not thinking_parts:
            continue
        am = {"role": role, "content": final if final is not None else ('' if tools else None)}
        if tools:
            am['tool_calls'] = tools
            if am.get('content') is None:
                am['content'] = ''
        if thinking_parts:
            am['reasoning_content'] = '\n'.join(thinking_parts)
        if role != 'tool':
            msgs.append(am)
    out = {"model": model, "messages": msgs, "stream": bool(body.get('stream')),
           "max_tokens": max(int(body.get('max_tokens') or 4096), 1)}
    if body.get('tools'):
        out['tools'] = [{"type": "function", "function": {
            "name": t['name'], "description": t.get('description', ''),
            "parameters": t.get('input_schema') or {},
        }} for t in body['tools'] if t.get('name')]
    return out


def _parse_dsml_from_text(text: str) -> tuple:
    """If upstream leaked MiniMax DSML tool markup into content, split to (clean_text, tool_use blocks)."""
    if not text or 'DSML' not in text.replace('\uff5c', '|'):
        return text or '', []
    normalized = text.replace('\uff5c', '|').replace('<|DSML|', '|DSML|')
    if '|DSML|tool_calls>' not in normalized:
        return text, []
    tools = []
    clean_parts = []
    OPEN = '|DSML|tool_calls>'
    CLOSE = '</|DSML|tool_calls>'
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
            clean_parts.append(normalized[s_idx:])
            break
        segment = normalized[s_idx:e_idx + len(CLOSE)]
        inv = re.findall(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|invoke>', segment)
        for name, inner in inv:
            params = dict(re.findall(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|parameter>', inner))
            tools.append({
                "type": "tool_use",
                "id": f"toolu_dsml_{int(time.time()*1000)}_{hash(name)%10000:04x}",
                "name": name,
                "input": params,
            })
        cursor = e_idx + len(CLOSE)
    clean = ''.join(clean_parts).strip()
    return clean, tools


def openai_to_anthropic(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    if text is None:
        text = ''
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    content = []
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    # Structured tool_calls (preferred) + DSML fallback if upstream leaked markup into content
    tool_calls = list(msg.get('tool_calls') or [])
    dsml_tools = []
    if isinstance(text, str) and 'DSML' in text.replace('\uff5c', '|'):
        text, dsml_tools = _parse_dsml_from_text(text)

    if text or (not tool_calls and not dsml_tools):
        content.append({"type": "text", "text": text if isinstance(text, str) else str(text)})

    for tc in tool_calls:
        fn = tc.get('function') or {}
        try:
            inp = json.loads(fn.get('arguments') or '{}')
        except Exception:
            inp = {"raw": fn.get('arguments', '')}
        content.append({
            "type": "tool_use",
            "id": tc.get('id') or f"toolu_{int(time.time()*1000)}",
            "name": fn.get('name', ''),
            "input": inp if isinstance(inp, dict) else {"value": inp},
        })
    content.extend(dsml_tools)

    if not content:
        content.append({"type": "text", "text": ""})
    fr = (data.get('choices') or [{}])[0].get('finish_reason')
    if tool_calls or dsml_tools:
        stop = "tool_use"
    else:
        stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(fr, "end_turn")
    u = data.get('usage') or {}
    return {"id": data.get('id') or f"msg_{int(time.time()*1000)}", "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": u.get('prompt_tokens', 0) or 0,
                      "output_tokens": u.get('completion_tokens', 0) or 0}}

# G11 fix: previous_response_id store for codex multi-turn server-side history
_RESPONSE_STORE: dict = {}



def _repair_orphan_tool_messages(messages):
    seen = set()
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    seen.add(tc["id"])
            out.append(m)
        elif m.get("role") == "tool" and (m.get("tool_call_id") not in seen):
            tcid = m.get("tool_call_id") or ""
            out.append({"role": "user", "content": f"Tool result{(' for ' + tcid) if tcid else ''}: {m.get('content', '')}"})
        else:
            out.append(m)
    return out
def responses_to_chat(body: dict) -> dict:
    model = _normalize_model(body.get('model') or '')
    msgs = []
    # G11: if previous_response_id references a stored conversation, prepend it
    prev = body.get('previous_response_id')
    if prev and prev in _RESPONSE_STORE:
        msgs.extend(_RESPONSE_STORE[prev])
    raw = body.get('input')
    if isinstance(raw, str):
        msgs.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, str):
                msgs.append({"role": "user", "content": it}); continue
            if not isinstance(it, dict):
                continue
            t = it.get('type')
            if t == 'function_call_output':
                outv = it.get('output', '')
                msgs.append({"role": "tool", "tool_call_id": it.get('call_id'),
                             "content": outv if isinstance(outv, str) else json.dumps(outv)})
            elif t == 'function_call':
                args = it.get('arguments', {})
                if not isinstance(args, str):
                    args = json.dumps(args or {})
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": it.get('call_id') or 'call_1', "type": "function",
                    "function": {"name": it.get('name', ''), "arguments": args}}]})
            else:
                role = it.get('role', 'user')
                if role == 'developer':
                    role = 'system'
                c = it.get('content', '')
                if isinstance(c, list):
                    c = " ".join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') in ('input_text', 'text', 'output_text'))
                msgs.append({"role": role or 'user', "content": c})
    if body.get('instructions'):
        if msgs and msgs[0].get('role') == 'system':
            msgs[0]['content'] = body['instructions'] + "\n\n" + str(msgs[0].get('content') or '')
        else:
            msgs.insert(0, {"role": "system", "content": body['instructions']})
    msgs = _repair_orphan_tool_messages(msgs)
    out = {"model": model, "messages": msgs, "stream": bool(body.get('stream', False))}
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = int(body['max_output_tokens'])
    elif body.get('max_tokens') is not None:
        out['max_tokens'] = int(body['max_tokens'])
    for k in ('temperature', 'top_p', 'tool_choice'):
        if body.get(k) is not None:
            v = body[k]
            if k in ('temperature', 'top_p'):
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    pass
            out[k] = v
    if body.get('tools'):
        tools = []
        for t in body['tools']:
            if not isinstance(t, dict):
                continue
            fn = t.get('function') if isinstance(t.get('function'), dict) else t
            name = fn.get('name') if isinstance(fn, dict) else None
            if not name:
                continue  # Codex name:null filter
            tools.append({"type": "function", "function": {
                "name": name, "description": fn.get('description', '') or '',
                "parameters": fn.get('parameters') or {},
            }})
        if tools:
            out['tools'] = tools
    return out

def chat_to_responses(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    output = []
    if reasoning:
        output.append({"id": f"rsn_{int(time.time()*1000)}", "type": "reasoning", "status": "completed", "text": reasoning})
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function') or {}
        output.append({"id": tc.get('id') or f"fc_{len(output)}", "type": "function_call", "status": "completed",
                       "call_id": tc.get('id'), "name": fn.get('name', ''), "arguments": fn.get('arguments', '') or ''})
    output.append({"id": f"msg_{int(time.time()*1000)}", "type": "message", "status": "completed", "role": "assistant",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]})
    u = data.get('usage') or {}
    return {"id": data.get('id') or f"resp_{int(time.time()*1000)}", "object": "response",
            "created_at": int(time.time()), "model": model, "status": "completed", "output": output,
            "usage": {"input_tokens": u.get('prompt_tokens', 0) or 0,
                      "output_tokens": u.get('completion_tokens', 0) or 0,
                      "total_tokens": u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}}


def _assistant_message_from_chat(data: dict, fallback_text: str = "", tool_accs=None) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if isinstance(data, dict) else {}
    content = msg.get('content')
    if content is None:
        content = fallback_text if fallback_text is not None else None
    tool_calls = msg.get('tool_calls') or []
    if tool_accs:
        tool_calls = [
            {"id": acc.get("call_id"), "type": "function", "function": {"name": acc.get("name", ""), "arguments": acc.get("args", "")}}
            for acc in tool_accs if acc
        ]
    out = {"role": "assistant", "content": content if content not in ("", None) else (None if tool_calls else "")}
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out

async def stream_passthrough(resp, key, heartbeat=True, terminal_done=True):
    """Yield upstream SSE bytes + proxy heartbeats; always release key/resp.

    For OpenAI-compatible streams, synthesize a final data: [DONE] on upstream
    EOF without one. Anthropic native pass-through disables this because its
    terminal event is message_stop, not [DONE].
    """
    last_hb = time.time()
    saw_done = False
    try:
        async for chunk in resp.content.iter_any():
            if isinstance(chunk, (bytes, bytearray)):
                if b"data: [DONE]" in chunk or b"data:[DONE]" in chunk:
                    saw_done = True
            else:
                if "data: [DONE]" in str(chunk) or "data:[DONE]" in str(chunk):
                    saw_done = True
            yield chunk
            if heartbeat and (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                yield b": heartbeat\n\n"
                last_hb = time.time()
        if terminal_done and not saw_done:
            yield b"data: [DONE]\n\n"
    finally:
        try:
            resp.release()
        except Exception:
            pass
        pool.release(key)

def start_env_watcher():
    if not HAS_WATCHDOG:
        return
    try:
        class EnvWatcher(FileSystemEventHandler):
            def on_modified(self, event):
                if '.env' in event.src_path:
                    load_dotenv(override=True)
                    logger.info('[env] .env hot-reloaded')
        obs = Observer()
        obs.schedule(EnvWatcher(), path=str(Path(__file__).parent.parent), recursive=False)
        obs.start()
        logger.info('[env] Watching .env')
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')

class _CatalogRequest:
    headers = {}


async def refresh_model_catalog_once():
    """Refresh the persistent OpenCode catalog independently of user traffic."""
    try:
        status, data, _ = await proxy_request_with_pool(
            "GET", f"{OPENCODE_BASE}/models", None, _CatalogRequest()
        )
        models_data = data.get("data") or data.get("models") or [] if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source="opencode:/models")
            MODEL_REGISTRY.register_catalog(models_data, revision="runtime-catalog")
            logger.info(f"[model-catalog] OpenCode refreshed {len(models_data)} models")
    except Exception as e:
        logger.warning(f"[model-catalog] OpenCode refresh failed: {e}")


async def model_catalog_refresh_loop():
    while True:
        await asyncio.sleep(max(60, MODEL_CATALOG_REFRESH_SEC))
        await refresh_model_catalog_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _session, _MODEL_REFRESH_TASK
    pool.load_from_env()
    start_env_watcher()
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or '').strip()
    if seed:
        set_dynamic_alias_target(seed, force=True)
    logger.info(f"wrapper-opencode starting on {BIND_HOST}:{LISTEN_PORT} base={OPENCODE_BASE} alias_target={get_dynamic_alias_target() or 'none'}")
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    yield
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _MODEL_REFRESH_TASK = None
    if _session is not None and not _session.closed:
        await _session.close()
    logger.info("Shutdown")

app = FastAPI(title="wrapper-opencode", version=VERSION, lifespan=lifespan)

@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and ("error" in detail or detail.get("type") == "error"):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": {"type": "api_error", "message": str(detail)}})
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
    allow_methods=['*'],
    allow_headers=['*'],
    expose_headers=['*'],
    allow_credentials=True,
)

def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return  # CORS preflight passes without auth
    # G10 fix: if BEARER_TOKEN is set, auth is mandatory and must match.
    # If client sends a token (even wrong) we MUST reject on mismatch.
    # If BEARER_TOKEN empty, remain open (backwards-compatible, logged).
    if not BEARER_TOKEN:
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            logger.warning("[auth] BEARER_TOKEN unset but client sent credentials — accepting open (insecure)")
        return
    auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if not token or token != BEARER_TOKEN:
        raise HTTPException(401, {"error": {"type": "authentication_error", "message": "Unauthorized"}})

@app.get("/health")
async def health():
    return {"status": "ok" if pool.available_keys > 0 else "degraded", "version": VERSION, "keys": pool.total_keys, "available": pool.available_keys, "free_only": free_only_enabled(), "dynamic_alias_target": get_dynamic_alias_target() or None, "base": OPENCODE_BASE}


@app.get("/ready")
async def ready(request: Request):
    _auth_check(request)
    try:
        status, data, _ = await proxy_request_with_pool("GET", f"{OPENCODE_BASE}/models", None, request)
        return {"ready": status == 200, "upstream_ok": status == 200, "status_code": status, "last_error": None if status == 200 else (data.get("error") if isinstance(data, dict) else str(data)), "keys": pool.total_keys, "available": pool.available_keys}
    except Exception as e:
        return _jr(503, {"ready": False, "upstream_ok": False, "last_error": str(e), "keys": pool.total_keys, "available": pool.available_keys})

@app.get("/v1/models")
async def models(request: Request):
    """Proxy Zen GET /models with the same multi-key retry semantics as runtime calls."""
    _auth_check(request)
    tgt = get_dynamic_alias_target()
    fallback_all = [
        {"id": "gpt-5.4-mini", "object": "model", "owned_by": "opencode-zen"},
        {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "opencode-zen"},
        {"id": "claude-haiku-4-5", "object": "model", "owned_by": "opencode-zen"},
        {"id": "claude-opus-4-6", "object": "model", "owned_by": "opencode-zen"},
        {"id": "big-pickle", "object": "model", "owned_by": "opencode-zen"},
        {"id": "mimo-v2.5-free", "object": "model", "owned_by": "opencode-zen"},
        {"id": "laguna-s-2.1-free", "object": "model", "owned_by": "opencode-zen"},
        {"id": "nemotron-3-ultra-free", "object": "model", "owned_by": "opencode-zen"},
        {"id": "deepseek-v4-flash-free", "object": "model", "owned_by": "opencode-zen"},
        {"id": "north-mini-code-free", "object": "model", "owned_by": "opencode-zen"},
    ]
    global _known_models
    for m in fallback_all:
        _known_models.add(m["id"])
    for alias in ("sonnet", "opus", "haiku"):
        if free_only_enabled() and not model_allowed(alias):
            continue
        fallback_all.append({"id": alias, "object": "model", "owned_by": "alias", "dynamic_alias": True, "rooted_model": tgt if tgt else None})
    if free_only_enabled():
        fallback_all = [m for m in fallback_all if model_allowed(m.get("id", ""))]
    fallback = {"object": "list", "data": fallback_all, "free_only": free_only_enabled(), "dynamic_alias_target": tgt or None}

    cached = MODEL_STORE.get_catalog(fresh_only=True)
    try:
        if cached:
            data = {'data': cached}
        else:
            status, data, _ = await proxy_request_with_pool("GET", f"{OPENCODE_BASE}/models", None, request)
            if status == 200 and isinstance(data, dict) and (data.get('data') or data.get('models')):
                MODEL_STORE.upsert_catalog(data.get('data') or data.get('models') or [], source='opencode:/models')
                MODEL_REGISTRY.register_catalog(data.get('data') or data.get('models') or [], revision='runtime-catalog')
            elif status != 200 or not isinstance(data, dict):
                stale = MODEL_STORE.get_catalog(fresh_only=False)
                if stale:
                    data = {'data': stale}
                else:
                    return fallback
        ids = {m.get('id') for m in (data.get('data') or [])}
        aliases_to_add = []
        for a in ("sonnet", "opus", "haiku"):
            if a not in ids:
                if free_only_enabled() and not model_allowed(a):
                    continue
                entry = {"id": a, "object": "model", "owned_by": "alias", "dynamic_alias": True}
                if tgt:
                    entry["rooted_model"] = tgt
                aliases_to_add.append(entry)
        (data.setdefault('data', [])).extend(aliases_to_add)
        for m in (data.get('data') or []):
            if isinstance(m, dict) and m.get('id'):
                _known_models.add(m.get('id', ''))
        if free_only_enabled():
            data['data'] = [m for m in (data.get('data') or []) if model_allowed(m.get('id', ''))]
        status_map = MODEL_STORE.status_map()
        for entry in data.get('data') or []:
            if isinstance(entry, dict) and entry.get('id'):
                state = status_map.get(entry['id'], {})
                entry['catalog_listed'] = True
                entry['availability_state'] = state.get('state', 'unknown')
                entry['availability_scope'] = 'account'
                entry['reason_code'] = state.get('reason_code', '')
                entry['checked_at'] = state.get('checked_at')
        data['free_only'] = free_only_enabled()
        data['dynamic_alias_target'] = tgt or None
        data['catalog_cached'] = bool(cached)
        return data
    except Exception as e:
        logger.warning(f"models: {e}")
        return fallback

@app.get("/v1/capabilities")
async def capabilities(request: Request):
    _auth_check(request)
    models_list = []
    try:
        model_response = await models(request)
        models_list = model_response.get("data", []) if isinstance(model_response, dict) else []
        global _known_models
        for m in models_list:
            if isinstance(m, dict) and m.get("id"):
                _known_models.add(m.get("id", ""))
    except Exception:
        models_list = MODEL_STORE.get_catalog(fresh_only=False)
    tgt = get_dynamic_alias_target()
    return {
        "object": "list",
        "models": [
            {
                "id": m.get("id") if isinstance(m, dict) else m,
                "capabilities": ["chat", "completion"],
                "streaming": True,
            }
            for m in models_list
        ],
        "summary": {"total": len(models_list), "by_type": {"chat": len(models_list)}},
        "dynamic_alias_target": tgt or None,
    }

@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}})
    return {"input_tokens": max(1, len(json.dumps(body)) // 4)}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat — routes to Zen /chat/completions (or native family if model demands it)."""
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}})
    if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
        return _jr(400, {"error": {"type": "invalid_request_error", "message": "max_tokens must be a positive integer"}})
    requested = body.get("model")  # transparent: never inject DEFAULT_MODEL
    if requested is not None:
        body["model"] = _normalize_model(requested)
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(body.get("model") or ""):
        return _jr(400, free_only_error(requested))
    if free_only_enabled() and body.get("model") and not model_allowed(body["model"]):
        return _jr(400, free_only_error(requested or body["model"]))

    for m in body.get('messages', []) or []:
        if isinstance(m, dict) and m.get('role') not in (None, 'system', 'user', 'assistant', 'tool', 'developer', 'function'):
            return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid role: {m.get('role')!r} (must be one of: system, user, assistant, tool, developer, function)"}})
        if isinstance(m, dict) and m.get('role') == 'tool' and not m.get('tool_call_id'):
            return _jr(400, {"error": {"type": "invalid_request_error", "message": "tool role requires tool_call_id"}})
    is_stream = bool(body.get("stream", False))

    # Prefer chat/completions; if model is responses/messages-native, still accept chat shape via conversion path upstream may reject — try chat first for openai-compatible clients
    family = _zen_family(body.get("model") or "")
    if family == "chat" or family == "google":
        url = f"{OPENCODE_BASE}/chat/completions" if family == "chat" else f"{OPENCODE_BASE}/models/{body.get('model') or ''}"
    else:
        # For GPT/Claude models Zen's native surface differs; still expose chat by converting through chat endpoint when available, else fall through
        url = f"{OPENCODE_BASE}/chat/completions"

    try:
        if is_stream:
            status, resp, key = await proxy_request_with_pool("POST", url, body, request, is_stream=True)
            if status != 200:
                return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
            return StreamingResponse(
                stream_passthrough(resp, key),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        status, data, _ = await proxy_request_with_pool("POST", url, body, request)
        if status != 200:
            return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
        await metrics.record_request(model=body.get("model"), path="/v1/chat/completions",
                                     prompt_tokens=(data.get("usage") or {}).get("prompt_tokens", 0),
                                     completion_tokens=(data.get("usage") or {}).get("completion_tokens", 0))
        return JSONResponse(_ensure_chat_message(data))
    except Exception as e:
        return _jr(502, {"error": {"message": str(e), "type": "api_error"}})

@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses — Zen native path is /responses for GPT* models.
    For chat-family models, translate Responses→Chat→Responses.
    """
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}})
    requested = body.get("model")  # transparent: never inject DEFAULT_MODEL
    model = _normalize_model(requested) if requested else ""
    if requested is not None:
        body["model"] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return _jr(400, free_only_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return _jr(400, free_only_error(requested or model))
    is_stream = bool(body.get("stream", False))
    family = _zen_family(model)

    try:
        if family == "responses":
            # Native Zen Responses passthrough
            url = f"{OPENCODE_BASE}/responses"
            if is_stream:
                status, resp, key = await proxy_request_with_pool("POST", url, body, request, is_stream=True)
                if status != 200:
                    return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
                return StreamingResponse(
                    stream_passthrough(resp, key),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                )
            try:
                status, data, _ = await proxy_request_with_pool("POST", url, body, request)
            except Exception as e:
                return _jr(502, {"error": {"message": f"Zen upstream error: {e}", "type": "api_error"}})
            if status != 200:
                err_data = data if isinstance(data, dict) else {"error": {"message": str(data)}}
                return _jr(status, err_data)
            # Zen may return {"type":"error",...} even with 200 in some paths — normalize
            if isinstance(data, dict) and data.get("type") == "error":
                return _jr(400, {"error": data.get("error", {"message": "Zen error", "type": "api_error"})})
            return JSONResponse(data)

        # Translate via chat/completions for non-GPT Zen models
        chat_body = responses_to_chat(body)
        chat_body["stream"] = is_stream
        url = f"{OPENCODE_BASE}/chat/completions"
        if is_stream:
            # Stream chat chunks → strict Responses SSE envelope for Codex.
            status, resp, key = await proxy_request_with_pool("POST", url, chat_body, request, is_stream=True)
            if status != 200:
                return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
            rid = f"resp_{int(time.time()*1000)}"
            async def gen():
                seq = 0
                acc_text = ""
                acc_usage = None
                buffer = b""
                tool_accs = []
                next_output_index = 1

                def emit(etype, payload):
                    nonlocal seq
                    seq += 1
                    return f"event: {etype}\ndata: {json.dumps({'type': etype, 'sequence_number': seq, **payload})}\n\n"

                def usage_obj():
                    if acc_usage:
                        return acc_usage
                    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

                def get_tool_acc(tc):
                    nonlocal next_output_index
                    idx = tc.get("index") if isinstance(tc.get("index"), int) else len(tool_accs)
                    acc = tool_accs[idx] if idx < len(tool_accs) else None
                    if acc is None:
                        acc = {"call_id": tc.get("id") or f"call_{idx}_{int(time.time()*1000)}", "name": "", "args": "", "output_index": next_output_index, "added": False}
                        next_output_index += 1
                        while len(tool_accs) <= idx:
                            tool_accs.append(None)
                        tool_accs[idx] = acc
                    if tc.get("id"):
                        acc["call_id"] = tc["id"]
                    return acc

                async def process_payload(payload: bytes):
                    nonlocal acc_text, acc_usage
                    if payload in (b"[DONE]", b"", b'"[DONE]"'):
                        return
                    try:
                        c = json.loads(payload)
                    except Exception:
                        return
                    if c.get("usage"):
                        u = c["usage"]
                        acc_usage = {"input_tokens": u.get("prompt_tokens", u.get("input_tokens", 0)) or 0,
                                     "output_tokens": u.get("completion_tokens", u.get("output_tokens", 0)) or 0,
                                     "total_tokens": u.get("total_tokens") or ((u.get("prompt_tokens", 0) or 0) + (u.get("completion_tokens", 0) or 0))}
                    d = ((c.get("choices") or [{}])[0].get("delta") or {})
                    if d.get("content"):
                        content = d["content"]
                        acc_text += content
                        yield emit("response.output_text.delta", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": content})
                    for tc in d.get("tool_calls") or []:
                        acc = get_tool_acc(tc)
                        fn = tc.get("function") or {}
                        if not acc["added"]:
                            acc["added"] = True
                            yield emit("response.output_item.added", {"output_index": acc["output_index"], "item": {"id": acc["call_id"], "type": "function_call", "status": "in_progress", "call_id": acc["call_id"], "name": acc["name"], "arguments": ""}})
                        if fn.get("name"):
                            acc["name"] += fn["name"]
                            yield emit("response.function_call.delta", {"item_id": acc["call_id"], "output_index": acc["output_index"], "delta": fn["name"], "name": acc["name"]})
                        if fn.get("arguments"):
                            acc["args"] += fn["arguments"]
                            yield emit("response.function_call.delta", {"item_id": acc["call_id"], "output_index": acc["output_index"], "delta": fn["arguments"]})

                try:
                    yield emit("response.created", {"response": {"id": rid, "model": model, "status": "in_progress"}})
                    yield emit("response.in_progress", {"response": {"id": rid, "status": "in_progress"}})
                    yield emit("response.output_item.added", {"output_index": 0, "item": {"id": "msg-1", "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
                    yield emit("response.content_part.added", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}})
                    async for chunk in resp.content.iter_any():
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            line = line.strip()
                            if not line.startswith(b"data:"):
                                continue
                            async for out in process_payload(line[5:].strip()):
                                yield out
                    # Flush final partial line if any.
                    tail = buffer.strip()
                    if tail.startswith(b"data:"):
                        async for out in process_payload(tail[5:].strip()):
                            yield out
                except Exception as e:
                    logger.error(f"[responses stream] {e}")
                    if not acc_text and not any(tool_accs):
                        acc_text = f"[upstream stream error: {e}]"
                        yield emit("response.output_text.delta", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": acc_text})
                finally:
                    try:
                        resp.release()
                    except Exception:
                        pass
                    pool.release(key)

                msg_item = {"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": acc_text, "annotations": []}]}
                yield emit("response.output_text.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "text": acc_text})
                yield emit("response.content_part.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": acc_text, "annotations": []}})
                yield emit("response.output_item.done", {"output_index": 0, "item": msg_item})
                outputs = [msg_item]
                completed_tools = [a for a in tool_accs if a]
                for acc in completed_tools:
                    fc_item = {"id": acc["call_id"], "type": "function_call", "status": "completed", "call_id": acc["call_id"], "name": acc["name"], "arguments": acc["args"]}
                    yield emit("response.output_item.done", {"output_index": acc["output_index"], "item": fc_item})
                    outputs.append(fc_item)
                yield emit("response.completed", {"response": {"id": rid, "object": "response", "created_at": int(time.time()), "model": model, "status": "completed", "output": outputs, "usage": usage_obj()}})
                yield "data: [DONE]\n\n"
                _RESPONSE_STORE[rid] = list(chat_body.get("messages", [])) + [_assistant_message_from_chat({}, acc_text, completed_tools)]
                if len(_RESPONSE_STORE) > 200:
                    _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        status, data, _ = await proxy_request_with_pool("POST", url, chat_body, request)
        if status != 200:
            return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
        resp_obj = chat_to_responses(model, data)
        # G11: store conversation for previous_response_id multi-turn
        rid_store = resp_obj.get("id")
        if rid_store:
            _RESPONSE_STORE[rid_store] = list(chat_body.get("messages", [])) + [_assistant_message_from_chat(data)]
            # keep store bounded
            if len(_RESPONSE_STORE) > 200:
                _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))
        # G11 also store under the request's response id if provided
        if body.get("previous_response_id") is None and body.get("id"):
            _RESPONSE_STORE[body["id"]] = chat_body.get("messages", [])
        return JSONResponse(resp_obj)
    except Exception as e:
        return _jr(502, {"error": {"message": str(e), "type": "api_error"}})


class AnthropicStreamState:
    """OpenAI chat SSE chunks → Anthropic Messages SSE (Claude Code native)."""

    def __init__(self, model: str):
        self.model = model
        self.index = -1
        self.message_started = False
        self.current_block = None  # thinking | text | tool_use
        self.tool_map = {}
        self.finished = False
        self.msg_id = f"msg_{int(time.time()*1000)}"

    def _sse(self, event: str, data: dict) -> str:
        payload = dict(data)
        if "type" not in payload:
            payload["type"] = event
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def start_events(self):
        if self.message_started:
            return []
        self.message_started = True
        return [self._sse("message_start", {
            "type": "message_start",
            "message": {
                "id": self.msg_id, "type": "message", "role": "assistant",
                "model": self.model, "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            },
        })]

    def _close_block(self):
        if self.current_block is None:
            return []
        ev = [self._sse("content_block_stop", {"type": "content_block_stop", "index": self.index})]
        self.current_block = None
        return ev

    def translate_chunk(self, chunk: dict):
        events = self.start_events()
        if not chunk or "choices" not in chunk:
            return events
        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta") or {}

        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            if self.current_block != "thinking":
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "thinking", "thinking": ""},
                }))
                self.current_block = "thinking"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "thinking_delta", "thinking": reason},
            }))

        if delta.get("content"):
            if self.current_block != "text":
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "text", "text": ""},
                }))
                self.current_block = "text"
            events.append(self._sse("content_block_delta", {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "text_delta", "text": delta["content"]},
            }))

        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            fn = tc.get("function") or {}
            if oi not in self.tool_map:
                events.extend(self._close_block())
                self.index += 1
                self.tool_map[oi] = self.index
                tid = tc.get("id") or f"toolu_{self.index}"
                events.append(self._sse("content_block_start", {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {
                        "type": "tool_use", "id": tid,
                        "name": fn.get("name") or "", "input": {},
                    },
                }))
                self.current_block = "tool_use"
            tidx = self.tool_map[oi]
            if fn.get("arguments"):
                events.append(self._sse("content_block_delta", {
                    "type": "content_block_delta", "index": tidx,
                    "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]},
                }))

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

    def force_done(self, stop="end_turn"):
        if self.finished:
            return []
        self.finished = True
        events = self._close_block()
        if not self.message_started:
            events.extend(self.start_events())
        events.append(self._sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
        }))
        events.append(self._sse("message_stop", {"type": "message_stop"}))
        return events


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    if not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0:
        return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens is required and must be a positive integer'}})
    sys_field = body.get('system')
    if sys_field is not None and not isinstance(sys_field, (str, list)):
        return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '"system" must be a string or array of content blocks'}})
    for t in body.get('tools', []) or []:
        if not isinstance(t.get('input_schema'), dict):
            return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tool.input_schema must be an object'}})
    requested = body.get("model")  # transparent: never inject DEFAULT_MODEL
    model = _normalize_model(requested) if requested else ""
    if requested is not None:
        body["model"] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return _jr(400, free_only_anthropic_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return _jr(400, free_only_anthropic_error(requested or model))
    is_stream = bool(body.get("stream", False))
    family = _zen_family(model)

    try:
        if family == "messages":
            url = f"{OPENCODE_BASE}/messages"
            if is_stream:
                status, resp, key = await proxy_request_with_pool("POST", url, body, request, is_stream=True)
                if status != 200:
                    return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(resp)}})
                return StreamingResponse(
                    stream_passthrough(resp, key, terminal_done=False),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                )
            status, data, _ = await proxy_request_with_pool("POST", url, body, request)
            if status != 200:
                return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(data)}})
            return JSONResponse(data)

        # Translate via chat/completions
        openai_body = anthropic_to_openai(body)
        openai_body["stream"] = is_stream
        url = f"{OPENCODE_BASE}/chat/completions"
        if is_stream:
            status, resp, key = await proxy_request_with_pool("POST", url, openai_body, request, is_stream=True)
            if status != 200:
                return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(resp)}})
            # Convert OpenAI SSE → Anthropic SSE (text + thinking + tool_use)
            state = AnthropicStreamState(model)
            async def gen():
                try:
                    for ev in state.start_events():
                        yield ev
                    buf = b""
                    async for chunk in resp.content.iter_any():
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            line = line.strip()
                            if not line.startswith(b"data:"):
                                continue
                            payload = line[5:].strip()
                            if payload in (b"[DONE]", b""):
                                for ev in state.force_done():
                                    yield ev
                                return
                            try:
                                c = json.loads(payload)
                            except Exception:
                                continue
                            for ev in state.translate_chunk(c):
                                yield ev
                    # upstream closed without [DONE]
                    for ev in state.force_done():
                        yield ev
                except Exception as e:
                    logger.error(f'[anthropic stream] {e}')
                    for ev in state.force_done():
                        yield ev
                finally:
                    try:
                        resp.release()
                    except Exception:
                        pass
                    pool.release(key)
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        status, data, _ = await proxy_request_with_pool("POST", url, openai_body, request)
        if status != 200:
            return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(data)}})
        return JSONResponse(openai_to_anthropic(model, data))
    except Exception as e:
        return _jr(502, {"type": "error", "error": {"type": "api_error", "message": str(e)}})

@app.get("/metrics")
async def get_metrics():
    return await metrics.summary()

@app.get("/metrics/prom")
async def prom():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(pool.prom_metrics() + metrics.prom_metrics(), media_type="text/plain; version=0.0.4")

@app.get("/metrics/model-status")
async def model_status():
    return {
        "provider": "opencode",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": MODEL_STORE.status_map(),
    }

@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    return _jr(404, {"error": {"message": f"Unsupported: /{path}", "type": "not_found_error"}})

def main():
    import uvicorn
    uvicorn.run("src.main:app", host=BIND_HOST, port=LISTEN_PORT, log_level="info")


if __name__ == "__main__":
    main()
