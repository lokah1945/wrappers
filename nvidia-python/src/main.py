#!/usr/bin/env python3
"""
main.py — FastAPI NVIDIA NIM API proxy server.
Migrated from index.js — functionally identical.

Routes:
  POST /v1/chat/completions
  POST /v1/responses
  POST /v1/messages (Anthropic)
  POST /v1/messages/count_tokens
  POST /v1/embeddings
  POST /v1/ranking
  POST /v1/images/generations
  POST /v1/images/edits
  GET  /v1/models
  GET  /v1/models/:model
  GET  /v1/capabilities
  GET  /v1/capabilities/params
  GET  /health
  GET  /stats
  GET  /metrics
  GET  /metrics/prom
  GET  /metrics/models
  GET  /metrics/models/timeseries
  GET  /metrics/keys
  GET  /metrics/activity
  GET  /metrics/rate-limits
  GET  /metrics/model-status
  GET  /metrics/chart/hourly
  GET  /metrics/chart/daily
  POST /metrics/reset
  POST /admin/heal-in-flight
  GET  /events (SSE)
  GET  /dashboard
  GET  /api/tags
  GET  /version
  GET  /props
  POST /api/show
  POST /v1/complete (legacy)
  POST /v1/engines (legacy)
  Catch-all proxy for Ollama/legacy paths
"""

import os
import sys
import json
import time
import uuid
import asyncio
import logging
import re as re_module
import threading
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple, AsyncGenerator

import aiohttp
from fastapi import FastAPI, Request, HTTPException, Response, status
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

# --------------------------------------------------------------------------
# MODEL VERIFICATION (full parity with Node.js audit)
# --------------------------------------------------------------------------
_unavailable_models: set = set()
_retired_models: set = set()
_model_status: dict = {}

async def probe_model(pool, model_id: str, timeout_ms: int = 120000) -> dict:
    """Probe a model for basic functionality (parity with Node verify)."""
    try:
        key = pool.peek_key()
        if not key:
            return {"ok": False, "status": 0, "reason": "no_key"}
        
        body = {"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
        headers = {"Authorization": f"Bearer {key.api_key}"}
        sess = await get_session() if 'get_session' in globals() else None
        
        # Use pool's session if available
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_ms/1000)) as s:
            async with s.post(f"{BASE_LLM}/v1/chat/completions", json=body, headers=headers) as resp:
                if resp.status < 400:
                    return {"ok": True, "status": resp.status, "reason": ""}
                text = await resp.text()
                return {"ok": False, "status": resp.status, "reason": text[:200]}
    except Exception as e:
        return {"ok": False, "status": 0, "reason": str(e)[:200]}

async def verify_models(pool):
    """Full model verification sweep (from Node audit)."""
    global _unavailable_models, _retired_models, _model_status
    ids = await pool.refresh_models(force=True) or []
    if not ids:
        return
    
    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    results = {}
    
    async def _probe(mid):
        async with sem:
            res = await probe_model(pool, mid, TTFT_TIMEOUT_MS)
            results[mid] = res
            if not res["ok"]:
                if res["status"] in (404, 410):
                    _retired_models.add(mid)
                _unavailable_models.add(mid)
            else:
                _unavailable_models.discard(mid)
                _retired_models.discard(mid)
            _model_status[mid] = res
    
    await asyncio.gather(*[_probe(mid) for mid in ids[:100]])  # cap for safety
    logger.info(f"[verify] sweep done: {len(_unavailable_models)} unavailable, {len(_retired_models)} retired")

async def verify_loop(pool):
    while True:
        try:
            await verify_models(pool)
            await asyncio.sleep(VERIFY_INTERVAL / 1000)
        except Exception as e:
            logger.error(f"[verify] loop error: {e}")
            await asyncio.sleep(60)


# ----------------------------------------------------------------------
# .env HOT RELOAD WATCHER (full Node parity)
# ----------------------------------------------------------------------
def start_env_watcher():
    """Start .env hot-reload watcher (exact parity with Node reloadDotenv + fs.watch)."""
    if not HAS_WATCHDOG:
        logger.warning('[env] watchdog not available; hot reload disabled')
        return
    try:
        class EnvWatcher(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith('.env') or event.src_path.endswith('/.env'):
                    load_dotenv(override=True)
                    # Re-apply any runtime config that depends on env (alias etc if needed)
                    logger.info('[env] .env reloaded (hot)')

        observer = Observer()
        watch_path = str(Path(__file__).parent.parent)
        observer.schedule(EnvWatcher(), path=watch_path, recursive=False)
        observer.start()
        logger.info('[env] Watching .env for hot reload')
    except Exception as e:
        logger.warning(f'[env] Failed to start watcher: {e}')

from .key_pool import KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL
from .anthropic_compat import (
    anthropic_to_openai,
    openai_to_anthropic,
    stream_openai_to_anthropic,
    estimate_input_tokens,
    anthropic_error,
    extract_internal_reasoning,
)
from .capabilities import (
    classify,
    describe,
    build_catalog,
    summarize,
    CAPABILITY_PARAMS,
    CURATED_GENAI,
    get_capability_params,
    MODEL_CONTEXT_WINDOWS,
    DEFAULT_CONTEXT_WINDOW,
    get_context_window,
)
from .responses_compat import ResponsesHandler
from .metrics import Metrics
from .registry import Registry

load_dotenv()

logger = logging.getLogger('wrapper-nvidia')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(message)s',
)

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9101'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
BASE_LLM = (os.environ.get('NVIDIA_BASE_URL') or NVIDIA_BASE_URL).rstrip('/')
BASE_GENAI = (os.environ.get('NVIDIA_GENAI_URL') or NVIDIA_GENAI_URL).rstrip('/')
BASE_NVCF = (os.environ.get('NVIDIA_NVCF_URL') or NVIDIA_NVCF_URL).rstrip('/')
DB_PATH = os.environ.get('METRICS_DB', str(Path(__file__).parent.parent / 'metrics.db'))
MAX_RETRIES = int(os.environ.get('QUIET_RETRIED_429', '3'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
HEADERS_TIMEOUT_MS = int(os.environ.get('HEADERS_TIMEOUT_MS', '120000'))
PRE_RESPONSE_TIMEOUT_MS = int(os.environ.get('PRE_RESPONSE_TIMEOUT_MS', '300000'))
TTFT_TIMEOUT_MS = int(os.environ.get('TTFT_TIMEOUT_MS', '120000'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT', '120'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '600'))
GEN_TIMEOUT_SEC = int(os.environ.get('GEN_TIMEOUT_SEC', '900'))
ANTI_SILENCE_TIMEOUT_MS = int(os.environ.get('ANTI_SILENCE_TIMEOUT_MS', '960000'))
INFLIGHT_SOFT_CAP = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))
LOAD_SHEDDING_ENABLED = os.environ.get('LOAD_SHEDDING_ENABLED', 'true').lower() != 'false'
VERIFY_CONCURRENCY = int(os.environ.get('VERIFY_CONCURRENCY', '8'))
VERIFY_INTERVAL = int(os.environ.get('VERIFY_INTERVAL', '600')) * 1000
VERIFY_ON_BOOT = os.environ.get('VERIFY_ON_BOOT', 'true').lower() != 'false'
MODEL_REFRESH_SEC = int(os.environ.get('MODEL_REFRESH_SEC', '600'))
MAX_STREAM_BUFFER_KB = int(os.environ.get('MAX_STREAM_BUFFER_KB', '512'))
MAX_STREAM_BUFFER = MAX_STREAM_BUFFER_KB * 1024
BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()
try:
    import importlib.metadata
    VERSION = f"{importlib.metadata.version('wrapper-nvidia')}-py"
except Exception:
    VERSION = '8.6.5-py'

REASONING_CONFIGS = [
    {'patterns': ['deepseek-v4', 'deepseek-r1', 'deepseek-reasoner'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True, 'thinking': True}, 'requires_reasoning': True},
    {'patterns': ['deepseek-coder'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['-reasoning', 'reason'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True, 'thinking': True}, 'requires_reasoning': True},
    {'patterns': ['thinkingmachines', 'inkling'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['qwen'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['glm'], 'mechanism': 'chat_template_kwargs', 'params': {'thinking': True}, 'requires_reasoning': False},
    {'patterns': ['phi-4'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['yi-'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['llama-4', 'llama-3.3-nemotron', 'llama-3.1-nemotron'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['gemma-3'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['gpt-oss', 'kimi', 'mistral-'], 'mechanism': 'reasoning_effort', 'params': {'effort': 'high'}, 'requires_reasoning': False},
    {'patterns': ['nemotron-3-ultra', 'nemotron-3-super', 'nemotron-3-', 'nemotron-4', 'llama-3.1-nemotron-ultra', 'llama-3.3-nemotron-super'], 'mechanism': 'nemotron_chat_template', 'params': {'enable_thinking': True, 'force_nonempty_content': True}, 'requires_reasoning': False},
    {'patterns': ['nemotron'], 'mechanism': 'reasoning_effort', 'params': {'effort': 'high'}, 'requires_reasoning': False},
]

DEPRECATED_MODEL_REDIRECTS = {
    'minimaxai/minimax-m2.5': 'minimaxai/minimax-m2.7',
    'minimaxai/minimax-m2.1': 'minimaxai/minimax-m2.7',
    'minimax/minimax-m2.5': 'minimaxai/minimax-m2.7',
    'z-ai/glm5': 'z-ai/glm-5.2',
    'z-ai/glm-5': 'z-ai/glm-5.2',
    'z-ai/glm-5.1': 'z-ai/glm-5.2',
    'zai/glm5': 'z-ai/glm-5.2',
    'zai/glm-5.1': 'z-ai/glm-5.2',
    'deepseek-ai/deepseek-v4': 'deepseek-ai/deepseek-v4-pro',
    'nvidia/llama-3.3-nemotron-super-49b': 'nvidia/llama-3.3-nemotron-super-49b-v1.5',
    'nvidia/llama-3.3-nemotron-super-49b-v1': 'nvidia/llama-3.3-nemotron-super-49b-v1.5',
}

ALIAS_TO_NIM = {}
DISCOVERY_TO_NIM = {}
DISCOVERY_PREFIX = 'claude-'
_unavailable_models: set = set()
_retired_models: set = set()
_sse_clients: set = set()
_in_flight = 0

DEFAULT_PARAMS = {}
PROACTIVE_DROP = set()
_WRAPPER_PARAMS = (os.environ.get('WRAPPER_PARAMS', 'temperature,top_p').split(',') if os.environ.get('WRAPPER_PARAMS') else ['temperature', 'top_p'])
for _p in _WRAPPER_PARAMS:
    _p = _p.strip()
    if _p:
        _dv = os.environ.get(f'DEFAULT_{_p.upper()}')
        if _dv:
            DEFAULT_PARAMS[_p] = _dv
PROACTIVE_DROP = set((os.environ.get('DROP_PARAMS', 'think').split(',') if os.environ.get('DROP_PARAMS') else ['think']))
PROACTIVE_DROP.update(['context_length', 'context_window', 'context_len', 'max_position_embeddings', 'max_context_length', 'max_input_tokens', 'max_output_tokens', 'token_limit'])

PROTECTED_PARAMS = {'messages', 'model', 'stream', 'tools', 'tool_choice', 'system'}


def find_reasoning_config(model_id: str) -> Optional[dict]:
    m = (model_id or '').lower()
    best = None
    best_len = -1
    for cfg in REASONING_CONFIGS:
        max_len = -1
        for p in cfg['patterns']:
            if p in m:
                max_len = max(max_len, len(p))
        if max_len > best_len:
            best_len = max_len
            best = cfg
    return best


def translate_thinking_to_nim(oai_body: dict, nim_model: str, anthropic_thinking: Any) -> None:
    if anthropic_thinking is None:
        return
    enabled = anthropic_thinking is True or (isinstance(anthropic_thinking, dict) and anthropic_thinking.get('type') != 'disabled')
    cfg = find_reasoning_config(nim_model)
    if not cfg:
        if not hasattr(translate_thinking_to_nim, '_unknown_logged'):
            translate_thinking_to_nim._unknown_logged = set()
        if nim_model not in translate_thinking_to_nim._unknown_logged:
            translate_thinking_to_nim._unknown_logged.add(nim_model)
            logger.warning(f'[REASONING] Model "{nim_model}" is NOT in REASONING_CONFIGS and client requested thinking.')
        return

    if cfg['mechanism'] == 'chat_template_kwargs':
        obj = {}
        for k, v in cfg['params'].items():
            obj[k] = v if enabled else False
        oai_body['chat_template_kwargs'] = {**(oai_body.get('chat_template_kwargs') or {}), **obj}
    elif cfg['mechanism'] == 'reasoning_effort':
        oai_body['reasoning_effort'] = cfg['params'].get('effort', 'high') if enabled else 'low'
    elif cfg['mechanism'] == 'nemotron_chat_template':
        obj = {}
        for k, v in cfg['params'].items():
            obj[k] = v if enabled else False
        oai_body['chat_template_kwargs'] = {**(oai_body.get('chat_template_kwargs') or {}), **obj}
        rb = (oai_body.get('extra_body', {}).get('reasoning_budget') if isinstance(oai_body.get('extra_body'), dict) else None) or \
             (oai_body.get('chat_template_kwargs', {}).get('reasoning_budget') if isinstance(oai_body.get('chat_template_kwargs'), dict) else None)
        if rb is not None:
            oai_body['extra_body'] = {**(oai_body.get('extra_body') or {}), 'reasoning_budget': rb}


def apply_default_reasoning(body: dict, model_id: str) -> None:
    has_explicit = bool(body.get('chat_template_kwargs') or body.get('reasoning_effort') or
                        (isinstance(body.get('extra_body'), dict) and (body['extra_body'].get('chat_template_kwargs') or body['extra_body'].get('reasoning_effort') or body['extra_body'].get('reasoning_budget'))))
    if has_explicit:
        return
    cfg = find_reasoning_config(model_id)
    if not cfg or not cfg.get('requires_reasoning'):
        return
    if cfg['mechanism'] == 'chat_template_kwargs':
        obj = {}
        for k, v in cfg['params'].items():
            obj[k] = v
        body['chat_template_kwargs'] = {**(body.get('chat_template_kwargs') or {}), **obj}
    elif cfg['mechanism'] == 'reasoning_effort':
        body['reasoning_effort'] = cfg['params'].get('effort', 'high')
    elif cfg['mechanism'] == 'nemotron_chat_template':
        obj = {}
        for k, v in cfg['params'].items():
            obj[k] = v
        body['chat_template_kwargs'] = {**(body.get('chat_template_kwargs') or {}), **obj}


def request_requires_reasoning(body: dict, model_id: str) -> bool:
    b = body or {}
    if b.get('chat_template_kwargs') and (b['chat_template_kwargs'].get('enable_thinking') or b['chat_template_kwargs'].get('thinking') or b['chat_template_kwargs'].get('force_nonempty_content')):
        return True
    if b.get('reasoning_effort'):
        return True
    if isinstance(b.get('extra_body'), dict) and (b['extra_body'].get('reasoning_budget') or b['extra_body'].get('reasoning_effort') or b['extra_body'].get('chat_template_kwargs')):
        return True
    if b.get('extended_thinking') or (b.get('thinking') and isinstance(b.get('thinking'), dict) and b['thinking'].get('type') != 'disabled'):
        return True
    if find_reasoning_config(model_id):
        return True
    return False


def is_reasoning_model(model_id: str) -> bool:
    return bool(find_reasoning_config(model_id))


def guard_stream_unsupported(body: dict, model_id: str) -> Optional[dict]:
    if not body or body.get('stream') is not True:
        return None
    cap = classify(model_id)
    if cap['type'] in ('chat', 'vision_chat', 'parse'):
        return None
    return {
        'status': 400,
        'data': {'error': {'message': f'Model "{model_id}" (type={cap["type"]}) does not support streaming via /v1/chat/completions. Streaming is only available for chat/vision_chat/parse models. Send stream=false or use a chat model.', 'type': 'invalid_request_error'}},
    }


def resolve_deprecated_redirect(requested_id: str) -> Optional[str]:
    if not requested_id:
        return None
    lower = str(requested_id).lower()
    if lower in DEPRECATED_MODEL_REDIRECTS:
        return DEPRECATED_MODEL_REDIRECTS[lower]
    for dep, cur in DEPRECATED_MODEL_REDIRECTS.items():
        stem = dep.split('/')[1]
        got = str(requested_id).lower().split('/')[1] if '/' in str(requested_id).lower() else ''
        if stem and got and got != cur.split('/')[1] and got.startswith(stem):
            return cur
    return None


def get_deprecated_redirect_info(model_id: str) -> Optional[dict]:
    if os.environ.get('DEPRECATED_MODEL_REDIRECT_ERROR') != '1':
        return None
    to = resolve_deprecated_redirect(model_id)
    if not to:
        return None
    return {'from': model_id, 'to': to}


def _strip_context_suffix(model_id: str) -> str:
    if not model_id:
        return model_id
    return re_module.sub(r'\[[0-9]+[mk]?\]$', '', model_id).strip()


def _norm_alias_key(s: str) -> str:
    return (s or '').lower().strip()


def _is_valid_nim_alias_target(id: str) -> bool:
    if not id or not isinstance(id, str):
        return False
    s = id.strip()
    if not s or ':' in s or ' ' in s:
        return False
    return bool(re_module.match(r'^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+$', s))


def _pick_alias_target(env_keys: list, fallback: str, family: str, pool: 'KeyPool' = None) -> str:
    """Resolve Claude Code alias target.

    Prefer explicit env override, else fallback if present in catalog, else a
    small known-good instruct model so aliases never hang on huge/retired ids.
    """
    for k in env_keys:
        v = os.environ.get(k)
        if not v:
            continue
        if _is_valid_nim_alias_target(v):
            return v.strip()
        logger.warning(f'[alias] Ignoring invalid {family} alias from {k}="{v}". Using default {fallback}')

    cached = []
    try:
        if pool is not None:
            cached = list(getattr(pool, 'models_cached', None) or [])
    except Exception:
        cached = []
    cached_l = {str(x).lower() for x in cached}

    def _available(mid: str) -> bool:
        if not mid:
            return False
        if not cached_l:
            return True  # catalog not warm yet — accept fallback
        return mid.lower() in cached_l

    if _available(fallback):
        return fallback

    # Family-oriented safe candidates (fast → larger)
    candidates = {
        'haiku': [
            'meta/llama-3.1-8b-instruct',
            'meta/llama-3.2-3b-instruct',
            'google/gemma-2-9b-it',
            'mistralai/mistral-7b-instruct-v0.3',
        ],
        'sonnet': [
            'meta/llama-3.3-70b-instruct',
            'meta/llama-3.1-70b-instruct',
            'meta/llama-3.1-8b-instruct',
            'mistralai/mistral-nemotron',
        ],
        'opus': [
            'meta/llama-3.1-405b-instruct',
            'meta/llama-3.3-70b-instruct',
            'meta/llama-3.1-70b-instruct',
            'meta/llama-3.1-8b-instruct',
        ],
    }.get(family, ['meta/llama-3.1-8b-instruct'])

    for c in candidates:
        if _available(c):
            logger.info(f'[alias] {family}: fallback {fallback} unavailable; using {c}')
            return c
    # Last resort: first chat-looking cached model
    for mid in cached:
        s = str(mid)
        if _is_valid_nim_alias_target(s) and 'embed' not in s.lower() and 'image' not in s.lower():
            logger.info(f'[alias] {family}: using catalog model {s}')
            return s
    return fallback or 'meta/llama-3.1-8b-instruct'


def load_alias_config(pool: KeyPool = None):
    global ALIAS_TO_NIM, DISCOVERY_TO_NIM
    haiku = _pick_alias_target(['CLAUDE_CODE_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'meta/llama-3.1-8b-instruct', 'haiku', pool)
    # Small default targets keep Claude Code aliases responsive; override via env if desired.
    sonnet = _pick_alias_target(['CLAUDE_CODE_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL'], 'meta/llama-3.1-8b-instruct', 'sonnet', pool)
    opus = _pick_alias_target(['CLAUDE_CODE_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL'], 'meta/llama-3.1-8b-instruct', 'opus', pool)

    mapping = {
        'haiku': haiku, 'sonnet': sonnet, 'opus': opus,
        'claude-haiku': haiku, 'claude-sonnet': sonnet, 'claude-opus': opus,
        'claude-3-5-haiku': haiku, 'claude-3-5-sonnet': sonnet, 'claude-3-opus': opus,
        'claude-3-haiku': haiku, 'claude-3-sonnet': sonnet,
        'claude-3-5-haiku-latest': haiku, 'claude-3-5-sonnet-latest': sonnet,
        'claude-3-5-haiku-20241022': haiku, 'claude-3-5-sonnet-20241022': sonnet,
        'claude-haiku-4-5': haiku, 'claude-sonnet-4-5': sonnet, 'claude-opus-4-5': opus,
        'claude-haiku-4-5-latest': haiku, 'claude-sonnet-4-5-latest': sonnet, 'claude-opus-4-5-latest': opus,
        'claude-sonnet-4': sonnet, 'claude-opus-4': opus, 'claude-haiku-4': haiku,
    }

    extra = os.environ.get('ANTHROPIC_ALIAS_MAP')
    if extra:
        try:
            parsed = json.loads(extra)
            for k, v in parsed.items():
                if k and v:
                    mapping[_norm_alias_key(k)] = v
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f'[alias] Failed to parse ANTHROPIC_ALIAS_MAP: {e}')

    ALIAS_TO_NIM = mapping

    all_ids = set((pool.models_cached if pool else []) or [])
    for c in CURATED_GENAI:
        all_ids.add(c)
    discovery_map = {}
    for id_val in all_ids:
        if id_val:
            discovery_map[discovery_alias(id_val)] = id_val
    DISCOVERY_TO_NIM = discovery_map
    logger.info(f'[alias] haiku={haiku} sonnet={sonnet} opus={opus}')


def discovery_alias(nim_id: str) -> str:
    return DISCOVERY_PREFIX + nim_id.replace('/', '-')


def resolve_target_model(requested_model: str) -> str:
    m = _strip_context_suffix(requested_model)
    if not m:
        return requested_model
    lower = m.lower()
    if m.startswith(DISCOVERY_PREFIX) and DISCOVERY_TO_NIM.get(m):
        return DISCOVERY_TO_NIM[m]
    if lower in ALIAS_TO_NIM:
        return ALIAS_TO_NIM[lower]
    redirect = resolve_deprecated_redirect(m)
    if redirect:
        return redirect
    return m


def is_model_unavailable(model_id: str) -> bool:
    return model_id in _unavailable_models


def route_upstream(path: str) -> str:
    if path.startswith('/v1/images') or path.startswith('/v1/audio') or path.startswith('/v1/video') or path.startswith('/v1/ranking') or path.startswith('/v1/infer'):
        return BASE_GENAI
    return BASE_LLM


def model_from_path(path: str) -> str:
    parts = path.strip('/').split('/')
    if len(parts) >= 2:
        return parts[-1]
    return ''


def forward_headers(request: Request) -> dict:
    headers = {}
    for key in ['x-forwarded-for', 'x-real-ip', 'user-agent', 'accept', 'anthropic-version', 'anthropic-beta', 'openai-beta']:
        val = request.headers.get(key)
        if val:
            headers[key] = val
    return headers


def client_ip(request: Request) -> str:
    xff = request.headers.get('x-forwarded-for', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.headers.get('x-real-ip', 'unknown')


def generate_request_id() -> str:
    return f"req_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def add_rate_limit_headers(resp_headers: dict, key_label: str) -> None:
    pass


def sanitize_nvidia_payload(body: dict) -> None:
    for p in PROACTIVE_DROP:
        if p not in PROTECTED_PARAMS:
            body.pop(p, None)


def ensure_nonempty_content(data: dict) -> None:
    if data.get('choices') and len(data['choices']) > 0:
        msg = data['choices'][0].get('message', {})
        if not msg.get('content') and not msg.get('tool_calls'):
            nr = extract_internal_reasoning(msg)
            if nr.get('reasoning'):
                msg['content'] = '[No text response; the model returned reasoning only.]'
            else:
                msg['content'] = ''


def pre_response_timeout_ms_for(model_id: str) -> int:
    return PRE_RESPONSE_TIMEOUT_MS



async def _safe_response_body(resp) -> dict:
    """Parse upstream body as JSON; fall back to text envelope (NIM sometimes returns text/plain errors)."""
    try:
        data = await resp.json(content_type=None)
        if isinstance(data, dict):
            return data
        return {'error': {'message': str(data)[:2000], 'type': 'api_error'}}
    except Exception:
        try:
            text = await resp.text()
        except Exception as e:
            text = str(e)
        return {'error': {'message': (text or f'HTTP {resp.status}')[:2000], 'type': 'api_error', 'code': resp.status}}

class Server:
    def __init__(self, app: FastAPI = None):
        self.app = app or FastAPI(title='wrapper-nvidia', docs_url=None, redoc_url=None, openapi_url=None)
        self.pool = KeyPool()
        self.metrics: Optional[Metrics] = None
        self.registry: Optional[Registry] = None
        self.responses_handler: Optional[ResponsesHandler] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._agent: Optional[aiohttp.TCPConnector] = None
        self._in_flight = 0
        self._sse_clients: set = set()
        self._start_time = time.time()

    async def init(self):
        self.pool.load_from_env()
        self._agent = aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS)
        self._session = aiohttp.ClientSession(connector=self._agent)
        self.pool.set_external_session(self._session)

        self.metrics = Metrics(DB_PATH)
        await self.metrics.init()

        self.registry = Registry()
        self.registry.set_external_agent(self._session)
        await self.registry.refresh(force=True)
        self.registry.start()

        # Warm NIM model catalog before resolving Claude Code aliases
        try:
            await self.pool.refresh_models(force=True)
        except Exception as e:
            logger.warning(f'[init] model catalog warm failed: {e}')

        load_alias_config(self.pool)

        self.responses_handler = ResponsesHandler({
            'pool': self.pool,
            'resolve_target_model': resolve_target_model,
            'proxy_openai': self.proxy_openai,
            'forward_headers': forward_headers,
            'BASE_LLM': BASE_LLM,
            'BASE_GENAI': BASE_GENAI,
            'describe': describe,
            'CURATED_GENAI': CURATED_GENAI,
            'translate_thinking_to_nim': translate_thinking_to_nim,
            'get_deprecated_redirect_info': get_deprecated_redirect_info,
            'guard_stream_unsupported': guard_stream_unsupported,
            'extract_internal_reasoning': extract_internal_reasoning,
        })

        self.pool.start_model_refresh()

        # Full model verification + env watcher (Node audit parity, production)
        if VERIFY_ON_BOOT:
            asyncio.create_task(verify_models(self.pool))
        asyncio.create_task(verify_loop(self.pool))
        start_env_watcher()

    def _register_routes(self):
        app = self.app

        @app.middleware('http')
        async def auth_middleware(request: Request, call_next):
            path = request.url.path
            method = request.method
            public_paths = ['/health', '/metrics/prom', '/', '/dashboard.html', '/dashboard', '/favicon.ico', '/events']
            is_public = (path in public_paths
                         or path == '/metrics/prom'
                         or (method == 'GET' and path == '/v1/models')
                         or (method == 'GET' and path.startswith('/v1/models/'))
                         or (method == 'GET' and path == '/v1/engines')
                         or (method == 'GET' and path.startswith('/v1/engines/'))
                         or (method == 'GET' and path in ('/version', '/api/version'))
                         or (method == 'GET' and path == '/api/tags')
                         or (method == 'GET' and path in ('/api/v1/models', '/models'))
                         or (method == 'GET' and path in ('/props', '/v1/props'))
                         or (method == 'GET' and path == '/v1/capabilities')
                         or (method == 'GET' and path == '/v1/capabilities/params'))

            if BEARER_TOKEN and not is_public:
                auth_header = (request.headers.get('authorization') or '').strip()
                api_key_header = (request.headers.get('x-api-key') or '').strip()
                token = auth_header.replace('Bearer ', '', 1) if auth_header.lower().startswith('bearer ') else auth_header or api_key_header
                if token != BEARER_TOKEN:
                    if path == '/v1/messages' or path.startswith('/v1/messages/'):
                        return JSONResponse(status_code=401, content={'type': 'error', 'error': {'type': 'authentication_error', 'message': 'Unauthorized'}})
                    return JSONResponse(status_code=401, content={'error': {'message': 'Unauthorized', 'type': 'authentication_error'}})

            return await call_next(request)

        @app.get('/health')
        async def health():
            snap = await self.metrics.summary('24h') if self.metrics else {}
            return {
                'status': 'ok' if self.pool.available_keys > 0 else 'degraded',
                'version': VERSION,
                'keys': self.pool.total_keys,
                'available': self.pool.available_keys,
                'models_cached': len(self.pool.models_cached),
                'uptime': int(time.time() - getattr(self, '_start_time', time.time())),
                **snap
            }

        @app.get('/version')
        async def version():
            return {'version': VERSION}

        @app.get('/api/version')
        async def api_version():
            return {'version': VERSION}

        @app.get('/')
        async def root():
            return {'status': 'ok', 'service': 'wrapper-nvidia'}

        @app.head('/')
        async def root_head():
            return Response(status_code=200)

        @app.get('/events')
        async def events(request: Request):
            async def event_stream():
                client_id = str(uuid.uuid4())
                self._sse_clients.add(request)
                yield 'event: connected\ndata: {"status":"ok"}\n\n'
                try:
                    while True:
                        await asyncio.sleep(3)
                        yield ': keepalive\n\n'
                except asyncio.CancelledError:
                    pass
                finally:
                    self._sse_clients.discard(request)
            return StreamingResponse(event_stream(), media_type='text/event-stream')

        @app.get('/stats')
        async def stats():
            s = await self.metrics.summary('24h')
            totals = await self.metrics.get_total_counts()
            return {**s, **totals, 'live_keys': self.pool.all_stats()}

        @app.get('/metrics/prom')
        async def prom_metrics():
            lines = []
            lines.append(self.pool.prom_metrics())
            s = await self.metrics.summary('24h')
            lines.extend([
                '# HELP wrapper_nvidia_requests_total Total requests',
                '# TYPE wrapper_nvidia_requests_total counter',
                f'wrapper_nvidia_requests_total {s.get("total_requests", 0)}',
                '# HELP wrapper_nvidia_tokens_total Total tokens',
                '# TYPE wrapper_nvidia_tokens_total counter',
                f'wrapper_nvidia_tokens_total {s.get("total_tokens", 0)}',
            ])
            return Response(content='\n'.join(lines), media_type='text/plain')

        @app.get('/metrics')
        async def metrics_route(request: Request):
            window = request.query_params.get('window', '24h')
            s = await self.metrics.summary(window)
            totals = await self.metrics.get_total_counts()
            return {**s, **totals, 'live_keys': self.pool.all_stats()}

        @app.get('/metrics/tokens')
        async def metrics_tokens(request: Request):
            window = request.query_params.get('window', '24h')
            s = await self.metrics.summary(window)
            return {
                'window': window,
                'prompt_tokens': s.get('prompt_tokens', 0),
                'completion_tokens': s.get('completion_tokens', 0),
                'cached_tokens': s.get('cached_tokens', 0),
                'total_tokens': s.get('total_tokens', 0),
                'cache_hit_pct': s.get('cache_hit_pct', 0),
            }

        @app.get('/metrics/models')
        async def metrics_models(request: Request):
            window = request.query_params.get('window', '24h')
            return {
                'window': window,
                'models': await self.metrics.get_per_model(window),
                'blocked_models': self.pool.blocked_models(),
            }

        @app.get('/metrics/models/timeseries')
        async def metrics_models_timeseries(request: Request):
            model = request.query_params.get('model', '')
            hours = int(request.query_params.get('hours', '24'))
            return {'model': model, 'hours': hours, 'data': await self.metrics.get_model_timeseries(model, hours)}

        @app.get('/metrics/keys')
        async def metrics_keys(request: Request):
            window = request.query_params.get('window', '24h')
            hist = await self.metrics.get_per_key(window)
            live = {}
            for k in self.pool.all_stats():
                live[k['label']] = k
            merged = []
            seen = set()
            for h in hist:
                label = h.get('key_label', 'unknown')
                merged.append({**h, 'live': live.get(label, {})})
                seen.add(label)
            for label, live_data in live.items():
                if label not in seen:
                    merged.append({
                        'key_label': label, 'requests': 0, 'total_tokens': 0, 'avg_latency_ms': 0,
                        'rate_limited_count': 0, 'total_retries': 0, 'live': live_data,
                    })
            return {'window': window, 'keys': merged}

        @app.get('/metrics/activity')
        async def metrics_activity(request: Request):
            limit = int(request.query_params.get('limit', '50'))
            offset = int(request.query_params.get('offset', '0'))
            rows = await self.metrics.recent_requests(limit, offset)
            return {'limit': limit, 'offset': offset, 'count': len(rows), 'rows': rows}

        @app.get('/metrics/rate-limits')
        async def metrics_rate_limits(request: Request):
            limit = int(request.query_params.get('limit', '100'))
            window = request.query_params.get('window', '24h')
            events = await self.metrics.rate_limit_events(limit)
            summary = await self.metrics.rate_limit_summary(window)
            full = await self.metrics.summary(window)
            return {
                'events': events, 'summary': summary,
                'blocked_models': self.pool.blocked_models(),
                'learned_model_limits': self.pool.summary().get('learned_model_limits', {}),
                'pacing': {
                    'paced_requests': full.get('paced_requests', 0),
                    'total_pacing_ms': full.get('total_pacing_ms', 0),
                },
                'live_keys': self.pool.all_stats(),
            }

        @app.post('/metrics/reset')
        async def metrics_reset():
            removed = await self.metrics.reset_all()
            await self.pool.reset_counters()
            return {'status': 'ok', 'reset': removed}

        @app.get('/metrics/model-status')
        async def metrics_model_status():
            status = await self.metrics.get_model_status()
            unavailable = await self.metrics.get_unavailable_models()
            verified_count = sum(1 for s in status.values() if s.get('ok'))
            return {
                'unavailable': list(unavailable),
                'unavailable_count': len(unavailable),
                'verified_count': verified_count,
                'checked': len(status),
                'learned_model_limits': self.pool.summary().get('learned_model_limits', {}),
            }

        @app.get('/metrics/chart/hourly')
        async def metrics_chart_hourly(request: Request):
            hours = int(request.query_params.get('hours', '24'))
            return {'hours': hours, 'data': await self.metrics.get_hourly_chart(hours)}

        @app.get('/metrics/chart/daily')
        async def metrics_chart_daily(request: Request):
            days = int(request.query_params.get('days', '30'))
            return {'days': days, 'data': await self.metrics.get_daily_chart(days)}

        @app.post('/admin/heal-in-flight')
        async def heal_in_flight():
            await self.pool.heal_in_flight()
            return {'status': 'ok', 'message': 'in_flight counters healed'}

        @app.get('/v1/capabilities')
        async def capabilities_route(request: Request):
            model_id = request.query_params.get('model', '')
            if model_id:
                ad_hoc = model_id not in (self.pool.models_cached or []) and model_id not in CURATED_GENAI
                d = describe(model_id, BASE_LLM, BASE_GENAI)
                if ad_hoc:
                    d['source'] = 'heuristic-adhoc'
                status_cap = await self.metrics.get_model_status()
                return enrich_model_metadata(model_id, d, status_cap)
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            status_list = await self.metrics.get_model_status()
            enriched = [enrich_model_metadata(d['id'], d, status_list) for d in catalog]
            return {
                'object': 'list', 'models': enriched,
                'summary': summarize(catalog),
                'hosts': {'llm': BASE_LLM, 'genai': BASE_GENAI, 'nvcf': BASE_NVCF},
            }

        @app.get('/v1/capabilities/params')
        async def capabilities_params(request: Request):
            model_id = request.query_params.get('model', '')
            capability = request.query_params.get('capability', '')
            if model_id:
                d = classify(model_id)
                return {'model': model_id, 'type': d['type'], 'supported_params': d.get('supported_params', {})}
            if capability:
                return {'type': capability, 'supported_params': get_capability_params(capability)}
            return CAPABILITY_PARAMS

        @app.get('/v1/models')
        async def models_route(request: Request):
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            status_list = await self.metrics.get_model_status()
            enriched = [enrich_model_metadata(d['id'], d, status_list) for d in catalog]
            return {'object': 'list', 'data': enriched}

        @app.get('/v1/models/{model_id:path}')
        async def model_info(model_id: str, request: Request):
            model_id = model_id.replace('%2F', '/').replace('%2f', '/')
            d = describe(model_id, BASE_LLM, BASE_GENAI)
            status_cap = await self.metrics.get_model_status()
            return enrich_model_metadata(model_id, d, status_cap)

        @app.get('/v1/engines')
        async def engines_route():
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            status_list = await self.metrics.get_model_status()
            enriched = [enrich_model_metadata(d['id'], d, status_list) for d in catalog]
            return {'object': 'list', 'data': enriched}

        @app.get('/v1/engines/{model_id:path}')
        async def engine_info(model_id: str):
            model_id = model_id.replace('%2F', '/').replace('%2f', '/')
            d = describe(model_id, BASE_LLM, BASE_GENAI)
            status_cap = await self.metrics.get_model_status()
            return enrich_model_metadata(model_id, d, status_cap)

        @app.get('/api/tags')
        async def api_tags():
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            models = []
            for d in catalog:
                mid = d['id']
                models.append({
                    'name': mid, 'model': mid, 'modified_at': '1970-01-01T00:00:00Z', 'size': 0, 'digest': '',
                    'details': {
                        'parent_model': '', 'format': 'gguf',
                        'family': mid.split('/')[0] if '/' in mid else mid,
                        'families': [mid.split('/')[0] if '/' in mid else mid],
                        'parameter_size': '', 'quantization_level': '',
                    },
                })
            return {'models': models}

        @app.get('/props')
        async def props():
            return {'system_prompt': '', 'default_generation_settings': {}, 'total_slots': 1}

        @app.get('/v1/props')
        async def v1_props():
            return {'system_prompt': '', 'default_generation_settings': {}, 'total_slots': 1}

        @app.post('/api/show')
        async def api_show():
            return {'license': '', 'modelfile': '', 'parameters': '', 'template': '', 'details': {}}

        @app.get('/favicon.ico')
        async def favicon():
            return Response(status_code=204)

        @app.get('/dashboard')
        async def dashboard():
            dashboard_path = Path(__file__).parent.parent / 'dashboard.html'
            if dashboard_path.exists():
                return HTMLResponse(content=dashboard_path.read_text())
            return HTMLResponse(content='<html><body><h1>wrapper-nvidia</h1><p>See /metrics, /metrics/prom, /v1/models</p></body></html>')

        @app.get('/dashboard.html')
        async def dashboard_html():
            dashboard_path = Path(__file__).parent.parent / 'dashboard.html'
            if dashboard_path.exists():
                return HTMLResponse(content=dashboard_path.read_text())
            return HTMLResponse(content='<html><body><h1>wrapper-nvidia</h1><p>See /metrics, /metrics/prom, /v1/models</p></body></html>')

        @app.post('/v1/chat/completions')
        async def chat_completions(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f'[JSON PARSE ERROR] completions: {e}')
                return JSONResponse(status_code=400, content={'error': {'message': f'Invalid JSON: {e}', 'type': 'invalid_request_error'}})

            dep = get_deprecated_redirect_info(body.get('model', ''))
            if dep:
                return JSONResponse(status_code=410, content={'error': {'message': f'Model "{dep["from"]}" has been renamed to "{dep["to"]}" in the NVIDIA NIM catalog. Update your request to use "{dep["to"]}".', 'type': 'invalid_request_error'}})

            body['model'] = resolve_target_model(body.get('model', ''))
            return await self._handle_chat_completions(body, request, raw)

        @app.post('/v1/complete')
        async def legacy_complete(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            if body.get('prompt') and not body.get('messages'):
                body['messages'] = [{'role': 'user', 'content': body['prompt']}]
                del body['prompt']
            body['model'] = resolve_target_model(body.get('model', ''))
            return await self._handle_chat_completions(body, request, raw)

        @app.post('/v1/responses')
        async def responses_api(request: Request):
            raw = await request.body()
            result, stream, status_code = await self.responses_handler.handle_responses_api(request, raw)
            if stream is not None:
                return StreamingResponse(stream, media_type='text/event-stream', headers={
                    'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no',
                })
            if result is not None and result.get('error'):
                sc = status_code or (400 if result['error'].get('type') == 'invalid_request_error' else 502)
                return JSONResponse(status_code=sc, content={'error': result['error']})
            if result is not None:
                return JSONResponse(status_code=200, content=result)
            return JSONResponse(status_code=500, content={'error': {'message': 'Unexpected error', 'type': 'server_error'}})

        @app.post('/v1/messages')
        async def anthropic_messages(request: Request):
            raw = await request.body()
            return await self._handle_anthropic_messages(raw, request)

        @app.post('/v1/messages/count_tokens')
        async def count_tokens(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            count = estimate_input_tokens(body)
            return {'input_tokens': count}

        @app.post('/v1/embeddings')
        async def embeddings(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            if not body.get('input_type'):
                if isinstance(body.get('input'), str):
                    body['input_type'] = 'query'
            model_id = resolve_target_model(body.get('model', ''))
            body['model'] = model_id
            if is_model_unavailable(model_id):
                return JSONResponse(status_code=404, content={'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}})
            return await self._proxy_post(request, body, raw, model_id, '/v1/embeddings', lambda key: f"{resolve_base(model_id)}/v1/embeddings")

        @app.post('/v1/ranking')
        async def ranking(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            model_id = resolve_target_model(body.get('model', ''))
            body['model'] = model_id
            if is_model_unavailable(model_id):
                return JSONResponse(status_code=404, content={'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}})
            return await self._proxy_post(request, body, raw, model_id, '/v1/ranking', lambda key: f"{resolve_base(model_id)}/v1/ranking")

        @app.post('/v1/images/generations')
        async def image_generations(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            requested_model = body.get('model', '')
            model_id = requested_model
            known = model_id in CURATED_GENAI or model_id in (self.pool.models_cached or []) or classify(model_id)['type'] == 'image'
            if not model_id or not known or is_model_unavailable(model_id):
                return JSONResponse(status_code=404, content={'error': {'message': f'Image model {model_id or "(missing)"} is not available', 'type': 'invalid_request_error'}})
            native_body = dict(body)
            for k in ['model', 'n', 'size', 'response_format', 'user', 'width', 'height']:
                native_body.pop(k, None)
            is_stability = any(x in model_id.lower() for x in ['stable-diffusion', 'sdxl', 'playground', 'kandinsky'])
            if is_stability:
                native_body['text_prompts'] = [{'text': body.get('prompt', ''), 'weight': 1}]
                native_body.pop('prompt', None)
            return await self._proxy_post(request, native_body, raw, model_id, '/v1/images/generations', lambda key: f"{BASE_GENAI}/v1/genai/{model_id}")

        @app.post('/v1/images/edits')
        async def image_edits(request: Request):
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return JSONResponse(status_code=400, content={'error': {'message': 'Invalid JSON', 'type': 'invalid_request_error'}})
            requested_model = body.get('model', '')
            model_id = requested_model
            known = model_id in CURATED_GENAI or model_id in (self.pool.models_cached or []) or classify(model_id)['type'] == 'image'
            if not model_id or not known or is_model_unavailable(model_id):
                return JSONResponse(status_code=404, content={'error': {'message': f'Image model {model_id or "(missing)"} is not available', 'type': 'invalid_request_error'}})
            native_body = dict(body)
            for k in ['model', 'n', 'size', 'response_format', 'user', 'width', 'height']:
                native_body.pop(k, None)
            return await self._proxy_post(request, native_body, raw, model_id, '/v1/images/edits', lambda key: f"{BASE_GENAI}/v1/genai/{model_id}")

        @app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'])
        async def catch_all(request: Request, path: str):
            return await self._handle_catch_all(request, path)

    def _normalize_path(self, path: str) -> str:
        if not path.startswith('/v1') and not path.startswith('/v2') and not path.startswith('/api'):
            stems = ['/chat/completions', '/completions', '/embeddings', '/models', '/engines',
                     '/images/generations', '/images/edits', '/images/variations',
                     '/audio/transcriptions', '/audio/translations', '/audio/speech',
                     '/moderations', '/responses', '/files', '/fine_tuning', '/batches',
                     '/ranking', '/infer']
            for stem in stems:
                if path == stem or path.startswith(stem + '/'):
                    return '/v1' + path
        return path

    async def _handle_chat_completions(self, body: dict, request: Request, raw: bytes):
        model_id = body.get('model', '')
        if is_model_unavailable(model_id):
            return JSONResponse(status_code=404, content={'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}})

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return JSONResponse(status_code=stream_guard['status'], content=stream_guard['data'])

        result = await self.proxy_openai(body, forward_headers(request), model_id, request)

        if result.get('stream'):
            return StreamingResponse(
                self._stream_chat(result, body, request),
                media_type='text/event-stream',
                headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
            )

        status_code = result.get('status', 200)
        data = result.get('data', {})
        if status_code != 200 and data.get('error'):
            return JSONResponse(status_code=status_code, content=data)
        ensure_nonempty_content(data)
        return JSONResponse(status_code=200, content=data)

    async def _stream_chat(self, result: dict, body: dict, request: Request):
        key = result.get('key')
        stream = result.get('stream')
        start_ms = result.get('start_ms', time.time() * 1000)
        heartbeat_ms = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
        max_buffer = int(os.environ.get('MAX_STREAM_BUFFER_KB', '512')) * 1024
        generated_chars = 0
        has_content = False
        stream_buffer = ''
        last_usage = ''

        try:
            async for chunk in stream:
                chunk_str = chunk.decode('utf-8', errors='replace') if isinstance(chunk, bytes) else chunk
                if not has_content:
                    for line in chunk_str.split('\n'):
                        t = line.strip()
                        if t.startswith('data:') and t != 'data:[DONE]' and t != 'data: [DONE]':
                            try:
                                c = json.loads(t[5:].strip())
                                d = c.get('choices', [{}])[0].get('delta', {}).get('content')
                                if isinstance(d, str):
                                    generated_chars += len(d)
                                    if d:
                                        has_content = True
                            except (json.JSONDecodeError, ValueError):
                                pass

                if '"usage"' in chunk_str:
                    last_usage = chunk_str[-65536:]

                stream_buffer += chunk_str
                if len(stream_buffer) > max_buffer:
                    stream_buffer = stream_buffer[-max_buffer:]

                yield chunk_str
        except Exception as e:
            logger.error(f'[stream error] _stream_chat: {e}')
        finally:
            if key:
                key.decrement_in_flight()
            self._in_flight = max(0, self._in_flight - 1)

        if not has_content and not re_module.search(r'data:\s*\[DONE\]', stream_buffer):
            friendly = f'The context/history for model "{body.get("model", "")}" is too large and exceeds the model\'s limit (or the upstream connection closed immediately). Please exit the current session and start a clean one.'
            yield f'data: {json.dumps({"error": {"message": friendly, "type": "invalid_request_error"}})}\n\n'

    async def _handle_anthropic_messages(self, raw: bytes, request: Request):
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            return JSONResponse(status_code=400, content={'error': anthropic_error('invalid_request_error', f'Invalid JSON: {e}')})

        model_id = resolve_target_model(body.get('model', ''))
        body['model'] = model_id

        if is_model_unavailable(model_id):
            return JSONResponse(status_code=404, content={'error': anthropic_error('not_found_error', f'Model {model_id} is retired or unavailable')})

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return JSONResponse(status_code=stream_guard['status'], content={'error': anthropic_error('invalid_request_error', stream_guard['data']['error']['message'])})

        try:
            openai_body = anthropic_to_openai(body, self.registry.get_official_context(model_id) if self.registry else None)
        except ValueError as e:
            return JSONResponse(status_code=400, content={'error': anthropic_error('invalid_request_error', str(e))})

        apply_default_reasoning(openai_body, model_id)
        openai_body['model'] = model_id

        if body.get('thinking') and isinstance(body['thinking'], dict):
            translate_thinking_to_nim(openai_body, model_id, body['thinking'])

        result = await self.proxy_openai(openai_body, forward_headers(request), model_id, request, metric_path='/v1/messages')

        if result.get('stream'):
            async def anthropic_stream():
                async for chunk in stream_openai_to_anthropic(result['stream'], model_id, {}, start_ms=result.get('start_ms', time.time() * 1000)):
                    yield chunk
            return StreamingResponse(
                anthropic_stream(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no',
                },
            )

        status_code = result.get('status', 200)
        data = result.get('data', {})
        if status_code != 200 and data.get('error'):
            err = data['error']
            return JSONResponse(status_code=status_code, content={'error': anthropic_error(err.get('type', 'api_error'), err.get('message', 'Unknown error'))})

        anthropic_resp = openai_to_anthropic(data, model_id, f"msg_{int(time.time())}")
        return JSONResponse(status_code=200, content=anthropic_resp)

    async def proxy_openai(self, body: dict, req_headers: dict, model: str, req: Request = None, metric_path: str = '/v1/chat/completions'):
        sanitize_nvidia_payload(body)
        model_id = body.get('model') or model or ''
        if is_model_unavailable(model_id):
            return {'status': 404, 'data': {'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}}}

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return stream_guard

        wants_reasoning = request_requires_reasoning(body, model_id)
        candidates = [model_id] + self._build_fallback_candidates(model_id, wants_reasoning)
        primary_body = json.loads(json.dumps(body))
        fallback_used = None

        for cand_model in candidates:
            body = json.loads(json.dumps(primary_body))
            body['model'] = cand_model
            model_id = cand_model

            if body.get('max_completion_tokens') is not None and body.get('max_tokens') is None:
                body['max_tokens'] = body['max_completion_tokens']
                del body['max_completion_tokens']

            if body.get('stream'):
                body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage': True}

            headers = dict(req_headers)

            for p, v in DEFAULT_PARAMS.items():
                if body.get(p) is None:
                    num = float(v)
                    body[p] = num if num == int(num) else v

            preserved = {}
            for p in ['chat_template_kwargs', 'reasoning_effort', 'extra_body', 'nvext']:
                if body.get(p) is not None:
                    preserved[p] = body[p]

            if isinstance(body.get('extra_body'), dict) and body['extra_body'].get('nvext'):
                preserved['nvext'] = {**(preserved.get('nvext') or {}), **body['extra_body']['nvext']}

            if isinstance(preserved.get('nvext'), dict) and 'stream' in preserved['nvext']:
                del preserved['nvext']['stream']

            if isinstance(preserved.get('extra_body'), dict) and isinstance(preserved['extra_body'].get('nvext'), dict):
                if 'stream' in preserved['extra_body']['nvext']:
                    del preserved['extra_body']['nvext']['stream']
                if not preserved['extra_body']['nvext']:
                    del preserved['extra_body']['nvext']

            for p in PROACTIVE_DROP:
                if p not in PROTECTED_PARAMS:
                    body.pop(p, None)

            reasoning_mechanism = (find_reasoning_config(model_id) or {}).get('mechanism')
            if reasoning_mechanism in ('reasoning_effort', 'nemotron_chat_template') and body.get('chat_template_kwargs'):
                del body['chat_template_kwargs']

            for p, v in preserved.items():
                body[p] = v

            target_url = f"{resolve_base(model_id)}/v1/chat/completions"

            start_ms = time.time() * 1000
            attempt = 0
            max_attempts = max(MAX_RETRIES + 1, self.pool.total_keys)

            while attempt < max_attempts:
                key_result = await self.pool.acquire(model_id)
                if not key_result:
                    if attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    return {'status': 503, 'data': {'error': {'message': f'All API keys exhausted — no capacity available for model {model_id}', 'type': 'server_error'}}}

                key = key_result['key']
                self._in_flight += 1
                key.increment_in_flight()

                try:
                    fwd_headers = {
                        'Authorization': f'Bearer {key.api_key}',
                        **headers,
                    }

                    is_streaming = bool(body.get('stream'))
                    is_gen = bool(re_module.search(r'images|genai|infer|audio|video|ranking', metric_path or ''))
                    # Production-grade timeout selection (parity with Node audit)
                    if is_streaming:
                        timeout_sec = max(STREAM_REQUEST_TIMEOUT_SEC, ANTI_SILENCE_TIMEOUT_MS // 1000)
                    elif is_gen:
                        timeout_sec = GEN_TIMEOUT_SEC
                    else:
                        timeout_sec = REQUEST_TIMEOUT_SEC

                    if body.get('stream'):
                        resp = await self._session.post(
                            target_url, json=body, headers=fwd_headers,
                            timeout=aiohttp.ClientTimeout(total=timeout_sec),
                        )
                        if resp.status == 429:
                            ra = int(resp.headers.get('retry-after', '65') or '65')
                            self._in_flight = max(0, self._in_flight - 1)
                            key.decrement_in_flight()
                            body_text = await resp.text()
                            await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                            if self.metrics:
                                await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                            attempt += 1
                            continue
                        if resp.status >= 400:
                            resp_body = await _safe_response_body(resp)
                            self._in_flight = max(0, self._in_flight - 1)
                            key.decrement_in_flight()
                            if attempt < max_attempts - 1:
                                attempt += 1
                                continue
                            return {'status': resp.status, 'data': resp_body}

                        # Keep in-flight until stream consumer finishes (_stream_chat / anthropic finally).
                        async def stream_wrapper():
                            try:
                                async for chunk, _ in resp.content.iter_chunks():
                                    yield chunk
                            finally:
                                try:
                                    await resp.release()
                                except Exception:
                                    pass

                        return {'stream': stream_wrapper(), 'key': key, 'start_ms': start_ms, 'status': 200}
                    else:
                        resp = await self._session.post(
                            target_url, json=body, headers=fwd_headers,
                            timeout=aiohttp.ClientTimeout(total=timeout_sec),
                        )

                        if resp.status == 429:
                            ra = int(resp.headers.get('retry-after', '65') or '65')
                            self._in_flight = max(0, self._in_flight - 1)
                            key.decrement_in_flight()
                            body_text = await resp.text()
                            await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                            if self.metrics:
                                await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                            attempt += 1
                            continue

                        resp_data = await _safe_response_body(resp)
                        self._in_flight = max(0, self._in_flight - 1)
                        key.decrement_in_flight()

                        if resp.status >= 400:
                            if attempt < max_attempts - 1:
                                attempt += 1
                                continue
                            return {'status': resp.status, 'data': resp_data}

                        if self.metrics:
                            await self.metrics.record_request(
                                model=model_id, key_label=key.label,
                                status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                                prompt_tokens=resp_data.get('usage', {}).get('prompt_tokens', 0),
                                completion_tokens=resp_data.get('usage', {}).get('completion_tokens', 0),
                                path=metric_path,
                            )
                        return {'status': resp.status, 'data': resp_data}

                except asyncio.TimeoutError:
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    attempt += 1
                    continue
                except Exception as e:
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    logger.error(f'[proxy_openai] error: {e}')
                    attempt += 1
                    continue

            return {'status': 503, 'data': {'error': {'message': f'All API keys exhausted for model {model_id}', 'type': 'server_error'}}}

        return {'status': 503, 'data': {'error': {'message': 'No candidate models available', 'type': 'server_error'}}}

    def _build_fallback_candidates(self, model_id: str, wants_reasoning: bool) -> list:
        if os.environ.get('MODEL_FALLBACK_ENABLED', 'true') == 'false':
            return []
        primary = classify(model_id)
        mtype = primary['type']
        cached = self.pool.models_cached or []
        seen = {str(model_id).lower()}
        cands = []
        for mid in cached:
            if str(mid).lower() in seen:
                continue
            c = classify(mid)
            if c['type'] != mtype:
                continue
            if wants_reasoning and not is_reasoning_model(mid):
                continue
            # Skip retired/unavailable
            if is_model_unavailable(mid):
                continue
            cands.append(mid)
        # Prefer smaller/faster instruct models first (avoid multi-minute fallbacks)
        def _rank(mid: str) -> tuple:
            s = str(mid).lower()
            score = 50
            if '8b' in s or '7b' in s or '3b' in s or 'mini' in s or 'nano' in s:
                score = 0
            elif '9b' in s or '12b' in s or '13b' in s:
                score = 1
            elif '27b' in s or '30b' in s or '32b' in s or '34b' in s:
                score = 2
            elif '70b' in s:
                score = 3
            elif '405b' in s or 'ultra' in s or '550b' in s:
                score = 9
            if 'instruct' in s:
                score -= 1
            return (score, len(s))
        cands.sort(key=_rank)
        return cands[:int(os.environ.get('MODEL_FALLBACK_MAX_HOPS', '2'))]

    def _resolve_base(self, model_id: str) -> str:
        return route_upstream(model_id)

    async def _proxy_post(self, request: Request, body: dict, raw: bytes, model_id: str, path: str, get_target_url):
        attempt = 0
        max_attempts = max(MAX_RETRIES + 1, self.pool.total_keys)
        is_streaming = bool(body.get('stream'))

        while attempt < max_attempts:
            key_result = await self.pool.acquire(model_id)
            if not key_result:
                return JSONResponse(status_code=503, content={'error': {'message': f'All API keys exhausted for model {model_id}', 'type': 'server_error'}})

            key = key_result['key']
            self._in_flight += 1
            key.increment_in_flight()
            start_ms = time.time() * 1000

            try:
                target_url = get_target_url(key)
                fwd_headers = {
                    'Authorization': f'Bearer {key.api_key}',
                    **forward_headers(request),
                    'Content-Type': 'application/json',
                }

                is_gen = bool(re_module.search(r'images|genai|infer|audio|video|ranking', path or ''))
                timeout_sec = STREAM_REQUEST_TIMEOUT_SEC if is_streaming else (GEN_TIMEOUT_SEC if is_gen else REQUEST_TIMEOUT_SEC)

                resp = await self._session.post(
                    target_url, json=body, headers=fwd_headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                )

                if resp.status == 429:
                    ra = int(resp.headers.get('retry-after', '65') or '65')
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    body_text = await resp.text()
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                    attempt += 1
                    continue

                resp_data = await resp.read()
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()

                if resp.status >= 400:
                    if attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    try:
                        err_data = json.loads(resp_data)
                    except (json.JSONDecodeError, ValueError):
                        err_data = {'error': {'message': resp_data.decode('utf-8', errors='replace'), 'type': 'api_error'}}
                    return JSONResponse(status_code=resp.status, content=err_data)

                if self.metrics:
                    await self.metrics.record_request(
                        model=model_id, key_label=key.label,
                        status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                        path=path,
                    )

                if is_streaming:
                    return StreamingResponse(
                        self._stream_proxy(resp, key),
                        media_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
                    )
                return JSONResponse(status_code=resp.status, content=json.loads(resp_data))

            except asyncio.TimeoutError:
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()
                attempt += 1
                continue
            except Exception as e:
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()
                logger.error(f'[_proxy_post] error: {e}')
                attempt += 1
                continue

        return JSONResponse(status_code=503, content={'error': {'message': f'All API keys exhausted for model {model_id}', 'type': 'server_error'}})

    async def _stream_proxy(self, resp, key):
        try:
            async for chunk, _ in resp.content.iter_chunks():
                yield chunk
        finally:
            self.pool.release_success(key)
            self._in_flight = max(0, self._in_flight - 1)

    async def _handle_catch_all(self, request: Request, path: str):
        path = self._normalize_path(path)
        method = request.method
        is_post = method in ('POST', 'PUT', 'PATCH')

        body = {}
        raw = b''
        if is_post:
            raw = await request.body()
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass

        requested_model = body.get('model') or model_from_path(path) or 'unknown'
        model_id = resolve_target_model(requested_model)
        if is_post:
            body['model'] = model_id

        if is_model_unavailable(model_id) or model_id == 'unknown':
            return JSONResponse(status_code=404, content={'error': {'message': model_id == 'unknown' and 'Unknown model — cannot route request' or f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}})

        target_host = route_upstream(path)
        target_url = f"{target_host}{path}"
        if request.url.query:
            target_url += f"?{request.url.query}"

        is_streaming = bool(body.get('stream') or (request.headers.get('accept') and 'text/event-stream' in request.headers['accept']))

        attempt = 0
        max_attempts = max(MAX_RETRIES + 1, self.pool.total_keys)

        while attempt < max_attempts:
            key_result = await self.pool.acquire(model_id)
            if not key_result:
                return JSONResponse(status_code=503, content={'error': {'message': f'All API keys exhausted for model {model_id}', 'type': 'server_error'}})

            key = key_result['key']
            self._in_flight += 1
            key.increment_in_flight()
            start_ms = time.time() * 1000

            try:
                fwd_headers = {
                    'Authorization': f'Bearer {key.api_key}',
                    **forward_headers(request),
                }
                if is_post:
                    fwd_headers['Content-Type'] = 'application/json'

                is_gen = bool(re_module.search(r'images|genai|infer|audio|video|ranking', path or ''))
                timeout_sec = STREAM_REQUEST_TIMEOUT_SEC if is_streaming else (GEN_TIMEOUT_SEC if is_gen else REQUEST_TIMEOUT_SEC)

                resp = await self._session.request(
                    method, target_url,
                    json=body if is_post else None,
                    headers=fwd_headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                )

                if resp.status == 429:
                    ra = int(resp.headers.get('retry-after', '65') or '65')
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    body_text = await resp.text()
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                    attempt += 1
                    continue

                resp_data = await resp.read()
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()

                if resp.status >= 400:
                    if attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    try:
                        err_data = json.loads(resp_data)
                    except (json.JSONDecodeError, ValueError):
                        err_data = {'error': {'message': resp_data.decode('utf-8', errors='replace'), 'type': 'api_error'}}
                    return JSONResponse(status_code=resp.status, content=err_data)

                if self.metrics:
                    await self.metrics.record_request(
                        model=model_id, key_label=key.label,
                        status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                        path=path,
                    )

                content_type = resp.headers.get('content-type', '')
                if 'text/event-stream' in content_type or is_streaming:
                    async def stream_catchall():
                        yield resp_data
                        async for chunk, _ in resp.content.iter_chunks():
                            yield chunk
                    return StreamingResponse(stream_catchall(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive'})

                try:
                    return JSONResponse(status_code=resp.status, content=json.loads(resp_data))
                except (json.JSONDecodeError, ValueError):
                    return Response(content=resp_data, status_code=resp.status, media_type=content_type)

            except asyncio.TimeoutError:
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()
                attempt += 1
                continue
            except Exception as e:
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()
                logger.error(f'[_handle_catch_all] error: {e}')
                attempt += 1
                continue

        return JSONResponse(status_code=503, content={'error': {'message': f'All API keys exhausted for model {model_id}', 'type': 'server_error'}})


def enrich_model_metadata(model_id: str, desc: dict, status: dict) -> dict:
    result = dict(desc)
    st = status.get(model_id, {})
    result['last_status'] = st.get('last_status', 0)
    result['ok'] = st.get('ok', True)
    result['reason'] = st.get('reason', '')
    result['verified'] = st.get('verified', False)
    return result


def resolve_base(model_id: str) -> str:
    return route_upstream(model_id)


server: Optional[Server] = None


async def get_server() -> Server:
    global server
    if server is None:
        server = Server()
        await server.init()
    return server


def create_app() -> FastAPI:
    global server
    app = FastAPI(title='wrapper-nvidia', docs_url=None, redoc_url=None, openapi_url=None)

    server = Server(app)
    server._register_routes()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await server.init()
        try:
            yield
        finally:
            if server:
                if server._session:
                    await server._session.close()
                if server._agent:
                    await server._agent.close()
                if server.metrics:
                    await server.metrics.close()
                if server.registry:
                    server.registry.stop()

    app.router.lifespan_context = lifespan
    return app


app = create_app()


def main():
    import uvicorn
    uvicorn.run(
        'src.main:app',
        host=BIND_HOST,
        port=LISTEN_PORT,
        log_level='info',
    )


if __name__ == '__main__':
    main()
