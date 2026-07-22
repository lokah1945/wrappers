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
import time
import threading
import asyncio
import logging
from typing import Optional, Dict, Any
from pathlib import Path
from contextlib import asynccontextmanager

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

load_dotenv()

logger = logging.getLogger('wrapper-opencode')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [opencode] %(message)s')

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9107'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
OPENCODE_BASE = os.environ.get('OPENCODE_BASE_URL', 'https://opencode.ai/zen/v1').rstrip('/')
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
DEFAULT_MODEL = os.environ.get('DEFAULT_MODEL', 'gpt-5.4-mini')
VERSION = '1.0.4-dynamic-alias'

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

def is_alias_name(model_id: str) -> bool:
    if not model_id:
        return False
    return str(model_id).lower().strip() in _ALIAS_NAME_SET

def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ''

def set_dynamic_alias_target(model_id: str) -> None:
    global _dynamic_alias_target
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if mid.lower().startswith('opencode/'):
        mid = mid.split('/', 1)[1]
    if not mid:
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
            timeout=aiohttp.ClientTimeout(total=900, sock_connect=30),
            connector=aiohttp.TCPConnector(limit=100, limit_per_host=50),
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
    set_dynamic_alias_target(m)
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
                timeout=aiohttp.ClientTimeout(total=900),
            )
            if resp.status >= 400:
                text = await resp.text()
                await resp.release()
                try:
                    data = json.loads(text)
                except Exception:
                    data = text
                return resp.status, _normalize_upstream_error(resp.status, data)
            return 200, resp
        async with sess.request(
            method, url, json=json_body, headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
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
        # Keep reasoning_content if present; do not move it into content (transparent)
        ch0["message"] = msg
        choices[0] = ch0
        data["choices"] = choices
    except Exception:
        pass
    return data

def _jr(status: int, content: dict):
    """JSONResponse with correct kw-only args (Starlette)."""
    return JSONResponse(status_code=status, content=content)

def _auth_headers(api_key: str, request: Request = None) -> dict:
    h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
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
                pass  # upstream-specific
        final = parts if len(parts) > 1 else (parts[0]['text'] if parts else ('' if tools else None))
        am = {"role": role, "content": final}
        if tools:
            am['tool_calls'] = tools
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

def openai_to_anthropic(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    content = []
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    # Always emit a text block when no tools (Anthropic clients expect content blocks)
    tool_calls = msg.get('tool_calls') or []
    if text or not tool_calls:
        content.append({"type": "text", "text": text or ""})
    for tc in tool_calls:
        fn = tc.get('function') or {}
        try:
            inp = json.loads(fn.get('arguments') or '{}')
        except Exception:
            inp = {"raw": fn.get('arguments', '')}
        content.append({"type": "tool_use", "id": tc.get('id') or f"toolu_{int(time.time()*1000)}",
                        "name": fn.get('name', ''), "input": inp if isinstance(inp, dict) else {"value": inp}})
    if not content:
        content.append({"type": "text", "text": ""})
    fr = (data.get('choices') or [{}])[0].get('finish_reason')
    stop = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens"}.get(fr, "end_turn")
    u = data.get('usage') or {}
    return {"id": data.get('id') or f"msg_{int(time.time()*1000)}", "type": "message", "role": "assistant",
            "model": model, "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": u.get('prompt_tokens', 0) or 0,
                      "output_tokens": u.get('completion_tokens', 0) or 0}}

def responses_to_chat(body: dict) -> dict:
    model = _normalize_model(body.get('model') or '')
    msgs = []
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
    out = {"model": model, "messages": msgs, "stream": bool(body.get('stream', False))}
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = int(body['max_output_tokens'])
    elif body.get('max_tokens') is not None:
        out['max_tokens'] = int(body['max_tokens'])
    for k in ('temperature', 'top_p', 'tool_choice'):
        if body.get(k) is not None:
            out[k] = body[k]
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

async def stream_passthrough(resp, key, heartbeat=True):
    """Yield upstream SSE bytes + proxy heartbeats; always release key/resp."""
    last_hb = time.time()
    try:
        async for chunk in resp.content.iter_any():
            yield chunk
            if heartbeat and (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                yield b": heartbeat\n\n"
                last_hb = time.time()
    finally:
        try:
            await resp.release()
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _session
    pool.load_from_env()
    start_env_watcher()
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or '').strip()
    if seed:
        set_dynamic_alias_target(seed)
    logger.info(f"wrapper-opencode starting on {BIND_HOST}:{LISTEN_PORT} base={OPENCODE_BASE} alias_target={get_dynamic_alias_target() or 'none'}")
    yield
    if _session is not None and not _session.closed:
        await _session.close()
    logger.info("Shutdown")

app = FastAPI(title="wrapper-opencode", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _auth_check(request: Request):
    if not BEARER_TOKEN:
        return
    auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if token != BEARER_TOKEN:
        raise HTTPException(401, {"error": {"type": "authentication_error", "message": "Unauthorized"}})

@app.get("/health")
async def health():
    return {"status": "ok", "version": VERSION, "keys": pool.total_keys, "available": pool.available_keys, "free_only": free_only_enabled(), "dynamic_alias_target": get_dynamic_alias_target() or None, "base": OPENCODE_BASE}

@app.get("/v1/models")
async def models(request: Request):
    """Proxy Zen GET /models (https://opencode.ai/zen/v1/models)."""
    _auth_check(request)
    key_result = await pool.acquire()
    # Even without keys, return curated aliases so Claude Code discovery works
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
        {"id": "sonnet", "object": "model", "owned_by": "alias"},
        {"id": "opus", "object": "model", "owned_by": "alias"},
        {"id": "haiku", "object": "model", "owned_by": "alias"},
    ]
    if free_only_enabled():
        fallback_all = [m for m in fallback_all if model_allowed(m.get("id", ""))]
    fallback = {"object": "list", "data": fallback_all, "free_only": free_only_enabled()}
    if not key_result:
        return fallback
    key = key_result['key']
    try:
        # Zen models endpoint is under base (base already includes /zen/v1)
        status, data = await proxy_request("GET", f"{OPENCODE_BASE}/models", None, _auth_headers(key.api_key, request))
        pool.release(key)
        if status != 200 or not isinstance(data, dict):
            return fallback
        # inject aliases (respect FREE_ONLY)
        ids = {m.get('id') for m in (data.get('data') or [])}
        tgt = get_dynamic_alias_target()
        for a in ("sonnet", "opus", "haiku"):
            if a not in ids and model_allowed(a):
                entry = {"id": a, "object": "model", "owned_by": "alias", "dynamic_alias": True}
                if tgt:
                    entry["rooted_model"] = tgt
                (data.setdefault('data', [])).append(entry)
        if free_only_enabled():
            data['data'] = [m for m in (data.get('data') or []) if model_allowed(m.get('id', ''))]
        data['free_only'] = free_only_enabled()
        data['dynamic_alias_target'] = tgt or None
        return data
    except Exception as e:
        pool.release(key)
        logger.warning(f"models: {e}")
        return fallback

@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    _auth_check(request)
    body = await request.json()
    return {"input_tokens": max(1, len(json.dumps(body)) // 4)}

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI Chat — routes to Zen /chat/completions (or native family if model demands it)."""
    _auth_check(request)
    body = await request.json()
    requested = body.get("model")  # transparent: never inject DEFAULT_MODEL
    if requested is not None:
        body["model"] = _normalize_model(requested)
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(body.get("model") or ""):
        return _jr(400, free_only_error(requested))
    if free_only_enabled() and body.get("model") and not model_allowed(body["model"]):
        return _jr(400, free_only_error(requested or body["model"]))
    is_stream = bool(body.get("stream", False))

    key_result = await pool.acquire()
    if not key_result:
        return _jr(503, {"error": {"message": "No capacity", "type": "server_error"}})
    key = key_result["key"]
    headers = _auth_headers(key.api_key, request)

    # Prefer chat/completions; if model is responses/messages-native, still accept chat shape via conversion path upstream may reject — try chat first for openai-compatible clients
    family = _zen_family(body.get("model") or "")
    if family == "chat" or family == "google":
        url = f"{OPENCODE_BASE}/chat/completions" if family == "chat" else f"{OPENCODE_BASE}/models/{body.get('model') or ''}"
    else:
        # For GPT/Claude models Zen's native surface differs; still expose chat by converting through chat endpoint when available, else fall through
        url = f"{OPENCODE_BASE}/chat/completions"

    try:
        if is_stream:
            status, resp = await proxy_request("POST", url, body, headers, is_stream=True)
            if status != 200:
                pool.release(key)
                return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
            return StreamingResponse(
                stream_passthrough(resp, key),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        status, data = await proxy_request("POST", url, body, headers)
        pool.release(key)
        if status != 200:
            return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
        await metrics.record_request(model=body.get("model"), path="/v1/chat/completions",
                                     prompt_tokens=(data.get("usage") or {}).get("prompt_tokens", 0),
                                     completion_tokens=(data.get("usage") or {}).get("completion_tokens", 0))
        return JSONResponse(_ensure_chat_message(data))
    except Exception as e:
        pool.release(key)
        return _jr(502, {"error": {"message": str(e), "type": "api_error"}})

@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses — Zen native path is /responses for GPT* models.
    For chat-family models, translate Responses→Chat→Responses.
    """
    _auth_check(request)
    body = await request.json()
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

    key_result = await pool.acquire()
    if not key_result:
        return _jr(503, {"error": {"message": "No capacity", "type": "server_error"}})
    key = key_result["key"]
    headers = _auth_headers(key.api_key, request)

    try:
        if family == "responses":
            # Native Zen Responses passthrough
            url = f"{OPENCODE_BASE}/responses"
            if is_stream:
                status, resp = await proxy_request("POST", url, body, headers, is_stream=True)
                if status != 200:
                    pool.release(key)
                    return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
                return StreamingResponse(
                    stream_passthrough(resp, key),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                )
            status, data = await proxy_request("POST", url, body, headers)
            pool.release(key)
            if status != 200:
                return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
            return JSONResponse(data)

        # Translate via chat/completions for non-GPT Zen models
        chat_body = responses_to_chat(body)
        chat_body["stream"] = is_stream
        url = f"{OPENCODE_BASE}/chat/completions"
        if is_stream:
            # Stream chat chunks → minimal Responses SSE envelope
            status, resp = await proxy_request("POST", url, chat_body, headers, is_stream=True)
            if status != 200:
                pool.release(key)
                return _jr(status, resp if isinstance(resp, dict) else {"error": {"message": str(resp)}})
            rid = f"resp_{int(time.time()*1000)}"
            async def gen():
                seq = 0
                def emit(etype, payload):
                    nonlocal seq
                    seq += 1
                    return f"event: {etype}\ndata: {json.dumps({'type': etype, 'sequence_number': seq, **payload})}\n\n"
                try:
                    yield emit("response.created", {"response": {"id": rid, "model": model, "status": "in_progress"}})
                    yield emit("response.in_progress", {"response": {"id": rid, "status": "in_progress"}})
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
                                continue
                            try:
                                c = json.loads(payload)
                            except Exception:
                                continue
                            d = ((c.get("choices") or [{}])[0].get("delta") or {})
                            if d.get("content"):
                                yield emit("response.output_text.delta", {"item_id": "msg-1", "output_index": 0, "delta": d["content"]})
                    yield emit("response.completed", {"response": {"id": rid, "model": model, "status": "completed"}})
                finally:
                    try:
                        await resp.release()
                    except Exception:
                        pass
                    pool.release(key)
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        status, data = await proxy_request("POST", url, chat_body, headers)
        pool.release(key)
        if status != 200:
            return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
        return JSONResponse(chat_to_responses(model, data))
    except Exception as e:
        pool.release(key)
        return _jr(502, {"error": {"message": str(e), "type": "api_error"}})

@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages — Zen native path is /messages for Claude* & some Qwen.
    For other families, translate A→O chat→A.
    """
    _auth_check(request)
    body = await request.json()
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

    key_result = await pool.acquire()
    if not key_result:
        return _jr(503, {"type": "error", "error": {"type": "api_error", "message": "No capacity"}})
    key = key_result["key"]
    headers = _auth_headers(key.api_key, request)
    # Anthropic clients often send x-api-key; Zen accepts Bearer
    headers.setdefault("anthropic-version", request.headers.get("anthropic-version") or "2023-06-01")

    try:
        if family == "messages":
            url = f"{OPENCODE_BASE}/messages"
            if is_stream:
                status, resp = await proxy_request("POST", url, body, headers, is_stream=True)
                if status != 200:
                    pool.release(key)
                    return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(resp)}})
                return StreamingResponse(
                    stream_passthrough(resp, key),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                )
            status, data = await proxy_request("POST", url, body, headers)
            pool.release(key)
            if status != 200:
                return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(data)}})
            return JSONResponse(data)

        # Translate via chat/completions
        openai_body = anthropic_to_openai(body)
        openai_body["stream"] = is_stream
        url = f"{OPENCODE_BASE}/chat/completions"
        if is_stream:
            status, resp = await proxy_request("POST", url, openai_body, headers, is_stream=True)
            if status != 200:
                pool.release(key)
                return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(resp)}})
            # Convert OpenAI SSE → Anthropic SSE (minimal text path)
            async def gen():
                msg_id = f"msg_{int(time.time()*1000)}"
                try:
                    yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':msg_id,'type':'message','role':'assistant','model':model,'content':[]}})}\n\n"
                    started = False
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
                                continue
                            try:
                                c = json.loads(payload)
                            except Exception:
                                continue
                            d = ((c.get("choices") or [{}])[0].get("delta") or {})
                            if d.get("content"):
                                if not started:
                                    yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}})}\n\n"
                                    started = True
                                yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':d['content']}})}\n\n"
                            fr = ((c.get("choices") or [{}])[0].get("finish_reason"))
                            if fr:
                                if started:
                                    yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
                                yield f"event: message_delta\ndata: {json.dumps({'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':c.get('usage') or {}})}\n\n"
                                yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"
                finally:
                    try:
                        await resp.release()
                    except Exception:
                        pass
                    pool.release(key)
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        status, data = await proxy_request("POST", url, openai_body, headers)
        pool.release(key)
        if status != 200:
            return _jr(status, {"type": "error", "error": {"type": "api_error", "message": str(data)}})
        return JSONResponse(openai_to_anthropic(model, data))
    except Exception as e:
        pool.release(key)
        return _jr(502, {"type": "error", "error": {"type": "api_error", "message": str(e)}})

@app.get("/metrics")
async def get_metrics():
    return await metrics.summary()

@app.get("/metrics/prom")
async def prom():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(pool.prom_metrics() + metrics.prom_metrics(), media_type="text/plain; version=0.0.4")

@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    return _jr(404, {"error": {"message": f"Unsupported: /{path}", "type": "not_found_error"}})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=BIND_HOST, port=LISTEN_PORT, log_level="info")