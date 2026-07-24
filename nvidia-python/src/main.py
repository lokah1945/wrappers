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
from typing import Optional, Any, Set

import aiohttp
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager

# Import the dependency-free shared model state layer even when this wrapper is
# launched from its own subdirectory by systemd/uvicorn.
try:
    from common.model_state import (
        ModelStateStore,
        classify_upstream_error,
        credential_fingerprint,
        error_text,
    )
    from common.model import LocalModelRegistry
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import (
        ModelStateStore,
        classify_upstream_error,
        credential_fingerprint,
        error_text,
    )
    from common.model import LocalModelRegistry

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
_model_state_store: Optional[ModelStateStore] = None

async def probe_model(pool, model_id: str, timeout_ms: int = 120000) -> dict:
    """Probe one model and retain the original provider error for classification.

    A probe is scoped to the credential used by the key pool.  It is not a
    global statement about the provider's catalog.
    """
    try:
        key = pool.peek_key()
        if not key:
            return {"ok": False, "status": 0, "reason": "no_key", "account_scope": "unknown"}

        body = {"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
        headers = {"Authorization": f"Bearer {key.api_key}"}
        account_scope = credential_fingerprint(key.api_key)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_ms/1000)) as s:
            async with s.post(f"{BASE_LLM}/v1/chat/completions", json=body, headers=headers) as resp:
                if 200 <= resp.status < 400:
                    return {"ok": True, "status": resp.status, "reason": "", "account_scope": account_scope}
                text = await resp.text()
                return {
                    "ok": False,
                    "status": resp.status,
                    "reason": text[:4000],
                    "account_scope": account_scope,
                }
    except Exception as e:
        return {"ok": False, "status": 0, "reason": str(e)[:4000], "account_scope": "unknown"}


async def verify_models(pool):
    """Verify models without converting account failures into global retirement."""
    global _unavailable_models, _retired_models, _model_status, _model_state_store
    ids = await pool.refresh_models(force=True) or []
    if not ids:
        return

    if _model_state_store:
        metadata = getattr(pool, "models_metadata", {}) or {}
        _model_state_store.upsert_catalog(
            [metadata.get(mid) or {"id": mid} for mid in ids],
            source="nvidia:/v1/models",
        )
        MODEL_REGISTRY.register_catalog([metadata.get(mid) or {"id": mid} for mid in ids], revision="runtime-catalog")

    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    results = {}

    async def _probe(mid):
        async with sem:
            res = await probe_model(pool, mid, TTFT_TIMEOUT_MS)
            results[mid] = res
            classification = classify_upstream_error(res.get("status", 0), res.get("reason", ""))
            res["state"] = classification["state"]
            res["reason_code"] = classification["reason_code"]
            if not res["ok"]:
                # Only an explicit provider EOL/retirement is global.  A 404
                # mentioning an account is account_unavailable and must never
                # enter _retired_models.
                if classification["state"] == "globally_retired":
                    _retired_models.add(mid)
                else:
                    _retired_models.discard(mid)
                _unavailable_models.add(mid)
            else:
                _unavailable_models.discard(mid)
                _retired_models.discard(mid)
                res["state"] = "available"
                res["reason_code"] = "OK"

            if _model_state_store:
                _model_state_store.record_status(
                    model_id=mid,
                    account_scope=res.get("account_scope", "unknown"),
                    state=res["state"],
                    status_code=res.get("status", 0),
                    reason_code=res.get("reason_code", ""),
                    reason_detail=res.get("reason", ""),
                    endpoint="/v1/chat/completions",
                )
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
    openai_to_anthropic as _openai_to_anthropic_impl,
    stream_openai_to_anthropic,
    estimate_input_tokens,
    anthropic_error,
    extract_internal_reasoning,
)


def openai_to_anthropic(*args, **kwargs):
    """Compatibility wrapper around anthropic_compat.openai_to_anthropic.

    Native nvidia code calls (openai_response, model, ...). Some cross-wrapper
    tooling imports src.main and calls the opencode/nous order (model, response).
    Accept both without changing the actual translator.
    """
    if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], dict):
        model, data = args[0], args[1]
        rest = args[2:]
        return _openai_to_anthropic_impl(data, model, *rest, **kwargs)
    return _openai_to_anthropic_impl(*args, **kwargs)


def _parse_dsml_from_text(text: str) -> tuple:
    """Split leaked MiniMax DSML tool markup into (clean_text, tool_use blocks)."""
    if not text or 'DSML' not in str(text).replace('\uff5c', '|'):
        return text or '', []
    normalized = str(text).replace('\uff5c', '|').replace('<|DSML|', '|DSML|')
    if '|DSML|tool_calls>' not in normalized:
        return text, []
    tools = []
    clean_parts = []
    open_tag = '|DSML|tool_calls>'
    close_tag = '</|DSML|tool_calls>'
    cursor = 0
    while True:
        s_idx = normalized.find(open_tag, cursor)
        if s_idx == -1:
            clean_parts.append(normalized[cursor:])
            break
        if s_idx > cursor:
            clean_parts.append(normalized[cursor:s_idx])
        e_idx = normalized.find(close_tag, s_idx)
        if e_idx == -1:
            # Incomplete DSML should not be leaked as-is to clients.
            break
        segment = normalized[s_idx:e_idx + len(close_tag)]
        for name, inner in re_module.findall(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|invoke>', segment):
            params = dict(re_module.findall(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|parameter>', inner))
            tools.append({
                'type': 'tool_use',
                'id': f'toolu_dsml_{int(time.time()*1000)}_{hash(name)%10000:04x}',
                'name': name,
                'input': params,
            })
        cursor = e_idx + len(close_tag)
    return ''.join(clean_parts).strip(), tools


class AnthropicStreamState:
    """Small OpenAI-chat-SSE → Anthropic-SSE state machine used by tests/tools."""

    def __init__(self, model: str):
        self.model = model
        self.index = -1
        self.message_started = False
        self.current_block = None
        self.tool_map = {}
        self.finished = False
        self.msg_id = f"msg_{int(time.time()*1000)}"

    def _sse(self, event: str, data: dict) -> str:
        payload = dict(data or {})
        payload.setdefault('type', event)
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def start_events(self):
        if self.message_started:
            return []
        self.message_started = True
        return [self._sse('message_start', {
            'type': 'message_start',
            'message': {
                'id': self.msg_id, 'type': 'message', 'role': 'assistant',
                'model': self.model, 'content': [], 'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0,
                          'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0},
            },
        })]

    def _close_block(self):
        if self.current_block is None:
            return []
        ev = [self._sse('content_block_stop', {'type': 'content_block_stop', 'index': self.index})]
        self.current_block = None
        return ev

    def translate_chunk(self, chunk: dict):
        events = self.start_events()
        if not isinstance(chunk, dict) or 'choices' not in chunk:
            return events
        ch = (chunk.get('choices') or [{}])[0]
        delta = ch.get('delta') or {}

        reason = delta.get('reasoning_content') or delta.get('reasoning')
        if isinstance(reason, str) and reason:
            if self.current_block != 'thinking':
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse('content_block_start', {
                    'type': 'content_block_start', 'index': self.index,
                    'content_block': {'type': 'thinking', 'thinking': ''},
                }))
                self.current_block = 'thinking'
            events.append(self._sse('content_block_delta', {
                'type': 'content_block_delta', 'index': self.index,
                'delta': {'type': 'thinking_delta', 'thinking': reason},
            }))

        content = delta.get('content')
        if isinstance(content, str) and content and 'DSML' in content.replace('\uff5c', '|'):
            content = None
        if content:
            if self.current_block != 'text':
                events.extend(self._close_block())
                self.index += 1
                events.append(self._sse('content_block_start', {
                    'type': 'content_block_start', 'index': self.index,
                    'content_block': {'type': 'text', 'text': ''},
                }))
                self.current_block = 'text'
            events.append(self._sse('content_block_delta', {
                'type': 'content_block_delta', 'index': self.index,
                'delta': {'type': 'text_delta', 'text': content},
            }))

        for tc in delta.get('tool_calls') or []:
            oi = tc.get('index', 0)
            fn = tc.get('function') or {}
            if oi not in self.tool_map:
                events.extend(self._close_block())
                self.index += 1
                self.tool_map[oi] = self.index
                tid = tc.get('id') or f'toolu_{self.index}'
                events.append(self._sse('content_block_start', {
                    'type': 'content_block_start', 'index': self.index,
                    'content_block': {'type': 'tool_use', 'id': tid, 'name': fn.get('name') or '', 'input': {}},
                }))
                self.current_block = 'tool_use'
            if fn.get('arguments'):
                events.append(self._sse('content_block_delta', {
                    'type': 'content_block_delta', 'index': self.tool_map[oi],
                    'delta': {'type': 'input_json_delta', 'partial_json': fn['arguments']},
                }))

        fr = ch.get('finish_reason')
        if fr and not self.finished:
            events.extend(self.force_done('tool_use' if (fr == 'tool_calls' or self.tool_map) else {'stop': 'end_turn', 'length': 'max_tokens', 'content_filter': 'refusal'}.get(fr, 'end_turn')))
        return events

    def force_done(self, stop='end_turn'):
        if self.finished:
            return []
        self.finished = True
        events = []
        if not self.message_started:
            events.extend(self.start_events())
        events.extend(self._close_block())
        if self.tool_map and stop == 'end_turn':
            stop = 'tool_use'
        events.append(self._sse('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': stop, 'stop_sequence': None},
            'usage': {'input_tokens': 0, 'output_tokens': 0,
                      'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0},
        }))
        events.append(self._sse('message_stop', {'type': 'message_stop'}))
        return events

    # opencode/nous naming compatibility
    done = force_done

from .capabilities import (
    classify,
    describe,
    build_catalog,
    summarize,
    CAPABILITY_PARAMS,
    CURATED_GENAI,
    get_capability_params,
)
from .responses_compat import ResponsesHandler
from .metrics import Metrics
from .registry import Registry
from . import alert_history
from . import loki_push

load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/nvidia-python/nvidia_py.log')
try:
    os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
    _log_file_handler = logging.FileHandler(LOG_FILE)
except Exception:
    LOG_FILE = '/tmp/wrapper-nvidia-python.log'
    _log_file_handler = logging.FileHandler(LOG_FILE)
logger = logging.getLogger('wrapper-nvidia')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(message)s',
    handlers=[
        _log_file_handler,
        logging.StreamHandler(sys.stdout),
    ],
)

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9101'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
BASE_LLM = (os.environ.get('NVIDIA_BASE_URL') or NVIDIA_BASE_URL).rstrip('/')
BASE_GENAI = (os.environ.get('NVIDIA_GENAI_URL') or NVIDIA_GENAI_URL).rstrip('/')
BASE_NVCF = (os.environ.get('NVIDIA_NVCF_URL') or NVIDIA_NVCF_URL).rstrip('/')
DB_PATH = os.environ.get('METRICS_DB', str(Path(__file__).parent.parent / 'metrics.db'))
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).parent.parent / 'model-state.db'))
MODEL_REGISTRY = LocalModelRegistry('nvidia')
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


DEFAULT_PARAMS = {}
PROACTIVE_DROP = set()
for _p in (os.environ.get('WRAPPER_PARAMS') or '').split(','):
    _p = _p.strip()
    if not _p:
        continue
    _dv = os.environ.get(f'DEFAULT_{_p.upper()}')
    if _dv is not None:
        # Numeric defaults must be floats, not strings (NVIDIA rejects "0.7")
        try:
            _dv = float(_dv)
        except (TypeError, ValueError):
            pass
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
    if _is_reasoning_injection_disabled(nim_model):
        return
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
    if _is_reasoning_injection_disabled(model_id):
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

ALIAS_TO_NIM = {}  # kept for metrics/debug: maps alias -> last dynamic target (informational)
DISCOVERY_TO_NIM = {}
DISCOVERY_PREFIX = 'claude-'

# Canonical Claude Code / Anthropic short names — NEVER hardcode a backing model.
# They resolve at request-time to the last concrete model the client called
# (e.g. minimaxai/minimax-m3 or z-ai/glm-5.2). Fully dynamic.
_ALIAS_NAME_SET = {
    'haiku', 'sonnet', 'opus',
    'claude-haiku', 'claude-sonnet', 'claude-opus',
    'claude-3-5-haiku', 'claude-3-5-sonnet', 'claude-3-opus',
    'claude-3-haiku', 'claude-3-sonnet',
    'claude-3-5-haiku-latest', 'claude-3-5-sonnet-latest',
    'claude-3-5-haiku-20241022', 'claude-3-5-sonnet-20241022',
    'claude-haiku-4-5', 'claude-sonnet-4-5', 'claude-opus-4-5',
    'claude-haiku-4-5-latest', 'claude-sonnet-4-5-latest', 'claude-opus-4-5-latest',
    'claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4',
    'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-opus-4-1', 'claude-opus-4-8',
    'claude-sonnet-4-20250514', 'claude-opus-4-20250514', 'claude-haiku-4-20250514',
}
_dynamic_alias_target: str = ''  # last concrete model id seen from any client request
_dynamic_alias_lock = threading.Lock()
_known_models: Set[str] = set()  # known model ids for alias validation (RC-2)


def _norm_alias_key(s: str) -> str:
    return (s or '').lower().strip()


def _is_valid_nim_alias_target(id: str) -> bool:
    if not id or not isinstance(id, str):
        return False
    s = id.strip()
    if not s or ':' in s or ' ' in s:
        return False
    return bool(re_module.match(r'^[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)+$', s))


def is_alias_name(model_id: str) -> bool:
    """True if model_id is a virtual alias (sonnet/haiku/claude-*), not a concrete provider id."""
    if not model_id or not isinstance(model_id, str):
        return False
    key = _norm_alias_key(_strip_context_suffix(model_id) or model_id)
    if key in _ALIAS_NAME_SET:
        return True
    # discovery form claude-<org>-<name> is handled separately
    if key.startswith('claude-') and '/' not in key:
        # bare claude-* without slash is treated as alias unless it is a discovery reverse map hit
        return True
    return False


def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ''


def set_dynamic_alias_target(model_id: str, force: bool = False) -> None:
    """Bind aliases only from explicit operator configuration.

    Concrete client requests never mutate alias state. ``force=True`` is used
    for the explicit environment seed during startup."""
    global _dynamic_alias_target, ALIAS_TO_NIM
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if not mid:
        return
    if not force and mid not in _known_models:
        logger.debug(f'[alias] ignoring unknown model {mid!r} — not in known model catalog')
        return
    with _dynamic_alias_lock:
        if _dynamic_alias_target != mid:
            logger.info(f'[alias] dynamic target bound → {mid} (all aliases now resolve here)')
        _dynamic_alias_target = mid
        # refresh informational map for metrics/debug
        ALIAS_TO_NIM = {a: mid for a in _ALIAS_NAME_SET}


def load_alias_config(pool: KeyPool = None):
    """No hardcoded alias→model map.

    Optional env seed only (operator choice, not code hardcode):
      DYNAMIC_ALIAS_TARGET=minimaxai/minimax-m3
    Discovery reverse-map still built from catalog (claude-org-name → org/name).
    """
    global DISCOVERY_TO_NIM, ALIAS_TO_NIM, _known_models
    all_ids = set((pool.models_cached if pool else []) or [])
    for c in CURATED_GENAI:
        all_ids.add(c)
    _known_models = set(s for s in all_ids if s and not is_alias_name(str(s)))
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or os.environ.get('ALIAS_DYNAMIC_TARGET') or '').strip()
    if seed and not is_alias_name(seed):
        set_dynamic_alias_target(seed, force=True)
    else:
        tgt = get_dynamic_alias_target()
        ALIAS_TO_NIM = {a: tgt for a in _ALIAS_NAME_SET} if tgt else {}

    discovery_map = {}
    for id_val in all_ids:
        if id_val and not is_alias_name(str(id_val)):
            discovery_map[discovery_alias(str(id_val))] = str(id_val)
    DISCOVERY_TO_NIM = discovery_map
    logger.info(f'[alias] dynamic mode on | target={get_dynamic_alias_target() or "(none — aliases require explicit binding)"} | discovery={len(DISCOVERY_TO_NIM)}')


def discovery_alias(nim_id: str) -> str:
    return DISCOVERY_PREFIX + nim_id.replace('/', '-')


def resolve_target_model(requested_model: str) -> str:
    """Transparent resolve with dynamic aliases.

    - Concrete id → pass through unchanged and never mutate alias state.
    - Alias → resolve only to an explicit operator binding; otherwise pass through unchanged.
    - No hardcoded or last-request default model under any alias.
    """
    m = _strip_context_suffix(requested_model)
    if not m:
        return requested_model or ''
    lower = m.lower()

    # Discovery reverse: claude-meta-llama-3.1-8b-instruct → meta/llama-3.1-8b-instruct
    if m.startswith(DISCOVERY_PREFIX) and DISCOVERY_TO_NIM.get(m):
        concrete = DISCOVERY_TO_NIM[m]
        return concrete

    # Concrete deprecated IDs are not silently redirected. The provider or
    # an explicit operator alias must decide what to do with them.

    # Virtual alias names → dynamic target
    if is_alias_name(m) or lower in _ALIAS_NAME_SET:
        tgt = get_dynamic_alias_target()
        if tgt:
            return tgt
        # No concrete model bound yet — do not invent one; pass alias through
        return m

    # Concrete provider model id: pass through unchanged.
    return m


def _csv_patterns(env_name: str, default: str = '') -> list:
    raw = os.environ.get(env_name, default) or ''
    return [x.strip().lower() for x in raw.split(',') if x.strip()]


def is_model_unavailable(model_id: str) -> bool:
    """Return True only for a confirmed local hard-block decision.

    Account-scoped 404s, rate limits, timeouts, capability mismatches, and
    unknown probe failures remain pass-through even when the legacy strict
    setting is enabled. The strict setting may only block a legacy ``unknown``
    state; explicit provider EOL is handled by ``_retired_models``.
    """
    if model_id in _retired_models:
        return True
    strict = os.environ.get('STRICT_BLOCK_UNAVAILABLE_MODELS', 'false').lower() in ('1', 'true', 'yes', 'on')
    if strict and model_id in _unavailable_models:
        state = (_model_status.get(model_id) or {}).get('state')
        return state == 'unknown'
    return False


def _is_reasoning_injection_disabled(model_id: str) -> bool:
    m = (model_id or '').lower()
    # NVIDIA build examples for moonshotai/kimi-k2.6 omit reasoning_effort and
    # the model may reject extra reasoning controls. Keep this provider-specific
    # skip configurable for future catalog changes.
    for pat in _csv_patterns('DISABLE_REASONING_INJECTION_PATTERNS', 'moonshotai/kimi-k2.6,kimi-k2.6'):
        if pat in m:
            return True
    return False


def _model_output_cap(model_id: str) -> Optional[int]:
    """Provider-specific max output cap to avoid upstream max_tokens errors.

    Format override: MODEL_MAX_TOKENS_CAPS='pattern:cap,other-pattern:cap'.
    Defaults include known NVIDIA build example caps.
    """
    caps = {
        'moonshotai/kimi-k2.6': 16384,
        'kimi-k2.6': 16384,
    }
    raw = os.environ.get('MODEL_MAX_TOKENS_CAPS', '') or ''
    for item in raw.split(','):
        if ':' not in item:
            continue
        pat, val = item.rsplit(':', 1)
        pat = pat.strip().lower()
        try:
            cap = int(val.strip())
        except (TypeError, ValueError):
            continue
        if pat and cap > 0:
            caps[pat] = cap
    m = (model_id or '').lower()
    for pat, cap in sorted(caps.items(), key=lambda kv: len(kv[0]), reverse=True):
        if pat in m:
            return cap
    return None


def clamp_max_tokens_for_model(body: dict, model_id: str) -> None:
    if not isinstance(body, dict):
        return
    cap = _model_output_cap(model_id)
    if not cap:
        return
    for key in ('max_tokens', 'max_completion_tokens'):
        if body.get(key) is None:
            continue
        try:
            val = int(body[key])
        except (TypeError, ValueError):
            continue
        if val > cap:
            logger.info(f'[model-cap] clamping {key} for {model_id}: {val} -> {cap}')
            body[key] = cap


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

def _normalize_upstream_error(status: int, data: dict, model_id: str = '') -> tuple:
    """Convert upstream NIM errors to OpenAI-compatible format (A1).

    Preserve account-scoped provider detail so Anthropic/Claude clients do not
    receive an opaque generic "unknown error".
    """
    if not isinstance(data, dict):
        data = {'error': {'message': error_text(data) or f'HTTP {status}', 'type': 'api_error'}}
    if status >= 400 and not isinstance(data.get('error'), dict):
        detail = data.get('detail') or data.get('message') or error_text(data) or f'HTTP {status}'
        data = {'error': {'message': str(detail)[:2000], 'type': 'api_error', 'code': status}}
    if status == 404:
        msg = (data.get('error') or {}).get('message', '') or ''
        lower = msg.lower()
        # NVIDIA's account-scoped function miss must remain visible to the
        # caller. It is not evidence of global model retirement.
        if 'not found for account' in lower or ('function' in lower and 'for account' in lower):
            return status, data
        if 'page not found' in lower or 'route' in lower:
            model_part = f' "{model_id}"' if model_id else ''
            return 400, {'error': {'message': f'Model{model_part} not found at upstream provider', 'type': 'invalid_request_error', 'code': 'model_not_found'}}
    return status, data

class Server:
    def __init__(self, app: FastAPI = None):
        global _model_state_store
        self.app = app or FastAPI(title='wrapper-nvidia', docs_url=None, redoc_url=None, openapi_url=None)
        self.pool = KeyPool()
        self.model_state = ModelStateStore('nvidia', MODEL_STATE_DB)
        _model_state_store = self.model_state
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

        EVENTS_FILE = os.environ.get('EVENTS_FILE', '/root/wrapper/nvidia/metrics_data/wrapper-events.jsonl')
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)

        async def _write_event(ev: dict):
            try:
                with open(EVENTS_FILE, 'a') as f:
                    f.write(json.dumps(ev) + '\n')
            except Exception:
                pass

        self.metrics.on_request(lambda ev: asyncio.create_task(_write_event(ev)))
        self.metrics.on_rate_limit(lambda ev: asyncio.create_task(_write_event(ev)))

        alert_history.SOURCE = EVENTS_FILE
        loki_push.SOURCE = EVENTS_FILE
        asyncio.create_task(alert_history.mode_daemon())
        asyncio.create_task(loki_push.daemon())

        self.registry = Registry()
        self.registry.set_external_agent(self._session)
        await self.registry.refresh(force=True)
        self.registry.start()

        # Hydrate the last good persistent catalog first.  This keeps model
        # discovery available during an upstream outage or process restart.
        try:
            cached_ids = self.model_state.get_ids(fresh_only=False)
            if cached_ids and not self.pool.models_cached:
                self.pool._models_cache = cached_ids
                self.pool._models_cache_ts = time.time()
                logger.info(f'[init] hydrated {len(cached_ids)} models from persistent catalog')
        except Exception as e:
            logger.warning(f'[init] persistent model catalog hydrate failed: {e}')

        # Warm NIM model catalog before resolving Claude Code aliases.
        try:
            ids = await self.pool.refresh_models(force=True)
            if ids:
                metadata = getattr(self.pool, 'models_metadata', {}) or {}
                self.model_state.upsert_catalog(
                    [metadata.get(mid) or {"id": mid} for mid in ids],
                    source='nvidia:/v1/models',
                )
                MODEL_REGISTRY.register_catalog([metadata.get(mid) or {"id": mid} for mid in ids], revision='runtime-catalog')
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

    def _model_status_view(self, metrics_status: Optional[dict] = None) -> dict:
        """Merge persistent account-scoped state into discovery metadata."""
        result = dict(metrics_status or {})
        try:
            for mid, state in self.model_state.status_map().items():
                result[mid] = {
                    'last_status': state.get('http_status', 0),
                    'ok': state.get('state') == 'available',
                    'reason': state.get('reason_detail', ''),
                    'verified': True,
                    'availability_state': state.get('state', 'unknown'),
                    'availability_scope': 'account',
                    'reason_code': state.get('reason_code', ''),
                    'checked_at': state.get('checked_at'),
                        }
        except Exception as e:
            logger.warning(f'[model-state] status read failed: {e}')
        return result

    def _record_model_response(self, model_id: str, key, status: int, payload: Any, endpoint: str):
        """Persist provider result with account scope, never raw credentials."""
        try:
            self.model_state.record_error(
                model_id=model_id,
                account_credential=getattr(key, 'api_key', None),
                status_code=status,
                payload=payload,
                endpoint=endpoint,
            )
        except Exception as e:
            logger.warning(f'[model-state] response record failed: {e}')

    def _register_routes(self):
        app = self.app

        @app.middleware('http')
        async def auth_middleware(request: Request, call_next):
            path = request.url.path
            method = request.method
            # CORS preflight must pass without auth so browser SDKs work
            if method == 'OPTIONS':
                return await call_next(request)
            public_paths = ['/health', '/ready', '/metrics/prom', '/', '/dashboard.html', '/dashboard', '/favicon.ico', '/events']
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
                    # D11: unknown paths return 404, not 401 (don't leak route info)
                    known_stems = ('/v1/chat/completions', '/v1/completions', '/v1/embeddings',
                                   '/v1/models', '/v1/engines', '/v1/images', '/v1/audio',
                                   '/v1/moderations', '/v1/responses', '/v1/files',
                                   '/v1/fine_tuning', '/v1/batches', '/v1/ranking', '/v1/infer',
                                   '/v1/messages', '/v1/messages/count_tokens',
                                   '/v1/capabilities', '/v1/capabilities/params',
                                   '/v2/', '/api/', '/v1/complete')
                    if path != '/' and not any(path == s.rstrip('/') or path.startswith(s) for s in known_stems):
                        return JSONResponse(status_code=404, content={'error': {'message': f'Unknown endpoint: {path}', 'type': 'invalid_request_error'}})
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

        @app.get('/ready')
        async def ready():
            return {
                'ready': self.pool.available_keys > 0,
                'upstream_ok': len(self.pool.models_cached) > 0,
                'keys': self.pool.total_keys,
                'available': self.pool.available_keys,
                'models_cached': len(self.pool.models_cached),
                'unavailable_models': len(_unavailable_models),
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
            status = self._model_status_view(await self.metrics.get_model_status())
            unavailable = {mid for mid, st in status.items() if st.get('availability_state') not in (None, 'available') or st.get('ok') is False}
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
                status_cap = self._model_status_view(await self.metrics.get_model_status())
                return enrich_model_metadata(model_id, d, status_cap)
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            status_list = self._model_status_view(await self.metrics.get_model_status())
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
            status_list = self._model_status_view(await self.metrics.get_model_status())
            enriched = [enrich_model_metadata(d['id'], d, status_list) for d in catalog]
            # Dynamic aliases: expose short names bound to current concrete target (if any)
            tgt = get_dynamic_alias_target()
            if tgt:
                seen = {e.get('id') for e in enriched}
                for alias in ('haiku', 'sonnet', 'opus'):
                    if alias not in seen:
                        enriched.append({
                            'id': alias, 'object': 'model', 'owned_by': 'alias',
                            'rooted_model': tgt, 'dynamic_alias': True,
                        })
            return {'object': 'list', 'data': enriched, 'dynamic_alias_target': tgt or None}

        @app.get('/v1/models/{model_id:path}')
        async def model_info(model_id: str, request: Request):
            model_id = model_id.replace('%2F', '/').replace('%2f', '/')
            d = describe(model_id, BASE_LLM, BASE_GENAI)
            status_cap = self._model_status_view(await self.metrics.get_model_status())
            return enrich_model_metadata(model_id, d, status_cap)

        @app.get('/v1/engines')
        async def engines_route():
            catalog = build_catalog(self.pool.models_cached or [], BASE_LLM, BASE_GENAI)
            status_list = self._model_status_view(await self.metrics.get_model_status())
            enriched = [enrich_model_metadata(d['id'], d, status_list) for d in catalog]
            return {'object': 'list', 'data': enriched}

        @app.get('/v1/engines/{model_id:path}')
        async def engine_info(model_id: str):
            model_id = model_id.replace('%2F', '/').replace('%2f', '/')
            d = describe(model_id, BASE_LLM, BASE_GENAI)
            status_cap = self._model_status_view(await self.metrics.get_model_status())
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

        def _serve_dashboard_html() -> HTMLResponse:
            """Serve dashboard.html, injecting the bearer token as a <meta> tag
            when auth is enabled (Node.js parity: index.js dashboard handler,
            lines ~3111-3113). The browser reads this meta tag via
            getAuthHeaders() and sends it on every /metrics API call, so the
            dashboard works behind auth WITHOUT a manual token entry.

            Without this injection the dashboard loads (it's in public_paths)
            but every /metrics* fetch returns 401 and all cards render '–'.
            """
            dashboard_path = Path(__file__).parent.parent / 'dashboard.html'
            if not dashboard_path.exists():
                return HTMLResponse(content='<html><body><h1>wrapper-nvidia</h1>'
                                            '<p>See /metrics, /metrics/prom, /v1/models</p></body></html>')
            html = dashboard_path.read_text()
            token = (BEARER_TOKEN or '').strip()
            if token:
                meta_tag = '<meta name="wrapper-bearer-token" content="' \
                    + token.replace('"', '&quot;') + '">'
                # Inject after the first <head> only (matches Node html.replace).
                html = html.replace('<head>', '<head>\n' + meta_tag, 1)
            return HTMLResponse(content=html)

        @app.get('/dashboard')
        async def dashboard():
            return _serve_dashboard_html()

        @app.get('/dashboard.html')
        async def dashboard_html():
            return _serve_dashboard_html()
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

            if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
                return JSONResponse(status_code=400, content={'error': {'message': 'max_tokens must be a positive integer', 'type': 'invalid_request_error'}})

            for m in body.get('messages', []) or []:
                if isinstance(m, dict) and m.get('role') not in (None, 'system', 'user', 'assistant', 'tool', 'developer', 'function'):
                    return JSONResponse(status_code=400, content={'error': {'message': f"Invalid role: {m.get('role')!r} (must be one of: system, user, assistant, tool, developer, function)", 'type': 'invalid_request_error'}})
                if isinstance(m, dict) and m.get('role') == 'tool' and not m.get('tool_call_id'):
                    return JSONResponse(status_code=400, content={'error': {'message': "tool role requires tool_call_id", 'type': 'invalid_request_error'}})

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
            import json as _json
            try:
                _b = _json.loads(raw)
                _temp = _b.get('temperature')
                _scan = []
                def _walk(o, path=''):
                    if isinstance(o, dict):
                        for k, v in o.items():
                            if k in ('temperature','top_p') and not isinstance(v, (int, float)):
                                _scan.append(f'{path}/{k}={v!r}')
                            _walk(v, f'{path}/{k}')
                    elif isinstance(o, list):
                        for i, v in enumerate(o):
                            _walk(v, f'{path}[{i}]')
                _walk(_b)
                logger.debug(f"[DBG responses] top_temp={_temp!r} suspicious={_scan} model={_b.get('model')}")
            except Exception as _e:
                logger.debug(f"[DBG responses] parse fail {_e}")
            try:
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
            except Exception as e:
                logger.exception(f"[responses_api] Unhandled exception: {e}")
                return JSONResponse(status_code=500, content={'error': {'message': f'Internal server error: {e}', 'type': 'server_error'}})

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

        # Cast numeric params that SDKs (Codex/OpenAI) may send as strings
        for _k in ('temperature', 'top_p', 'top_k', 'presence_penalty', 'frequency_penalty', 'min_p'):
            if body.get(_k) is not None:
                try:
                    body[_k] = float(body[_k])
                except (TypeError, ValueError):
                    pass

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return JSONResponse(status_code=stream_guard['status'], content=stream_guard['data'])

        clamp_max_tokens_for_model(body, model_id)
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
        stream = result.get('stream')
        max_buffer = int(os.environ.get('MAX_STREAM_BUFFER_KB', '512')) * 1024
        generated_chars = 0
        has_content = False
        stream_buffer = ''
        saw_done = False
        stream_error = None

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

                if re_module.search(r'data:\s*\[DONE\]', chunk_str):
                    saw_done = True

                stream_buffer += chunk_str
                if len(stream_buffer) > max_buffer:
                    stream_buffer = stream_buffer[-max_buffer:]

                yield chunk_str
        except Exception as e:
            stream_error = e
            logger.error(f'[stream error] _stream_chat: {e}')

        if not saw_done and not re_module.search(r'data:\s*\[DONE\]', stream_buffer):
            if not has_content:
                friendly = f"The context/history for model '{body.get('model', '')}' is too large and exceeds the model's limit (or the upstream connection closed immediately). Please exit the current session and start a clean one."
                if stream_error:
                    friendly = f'{friendly} Upstream stream error: {stream_error}'
                yield f'data: {json.dumps({"error": {"message": friendly, "type": "invalid_request_error"}})}\n\n'
            elif stream_error:
                yield f'data: {json.dumps({"error": {"message": f"Upstream stream interrupted: {stream_error}", "type": "api_error"}})}\n\n'
            # Always terminate OpenAI Chat SSE explicitly. Several agents wait
            # for [DONE] and otherwise stop mid-run on upstream EOF.
            yield 'data: [DONE]\n\n'

    async def _handle_anthropic_messages(self, raw: bytes, request: Request):
        anthro_version = (request.headers.get('anthropic-version') or '').strip()
        # Claude Code always sends this; default for other Anthropic-compatible clients
        if not anthro_version:
            anthro_version = '2023-06-01'

        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', f'Invalid JSON: {e}'))
        if not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0:
            return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', 'max_tokens is required and must be a positive integer'))

        sys_field = body.get('system')
        if sys_field is not None and not isinstance(sys_field, (str, list)):
            return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', '"system" must be a string or array of content blocks'))

        for t in body.get('tools', []) or []:
            if not isinstance(t.get('input_schema'), dict):
                return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', 'tool.input_schema must be an object'))

        model_id = resolve_target_model(body.get('model', ''))
        body['model'] = model_id

        if is_model_unavailable(model_id):
            return JSONResponse(status_code=404, content=anthropic_error('not_found_error', f'Model {model_id} is retired or unavailable'))

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return JSONResponse(status_code=stream_guard['status'], content=anthropic_error('invalid_request_error', stream_guard['data']['error']['message']))

        try:
            openai_body = anthropic_to_openai(body, self.registry.get_official_context(model_id) if self.registry else None)
        except ValueError as e:
            return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', str(e)))

        # anthropic_to_openai may return a structured error instead of raising
        if isinstance(openai_body, dict) and openai_body.get('error'):
            err = openai_body['error']
            return JSONResponse(
                status_code=400,
                content=anthropic_error(err.get('type', 'invalid_request_error'), err.get('message', 'Invalid request')),
            )

        apply_default_reasoning(openai_body, model_id)
        openai_body['model'] = model_id

        if body.get('thinking') and isinstance(body['thinking'], dict):
            translate_thinking_to_nim(openai_body, model_id, body['thinking'])

        clamp_max_tokens_for_model(openai_body, model_id)
        result = await self.proxy_openai(openai_body, forward_headers(request), model_id, request, metric_path='/v1/messages')

        expect_thinking = bool(
            isinstance(body.get('thinking'), dict) and body['thinking'].get('type') == 'enabled'
        ) or bool(body.get('extended_thinking'))
        try:
            input_tok_est = estimate_input_tokens(body)
        except Exception:
            input_tok_est = 0

        if result.get('stream'):
            async def anthropic_stream():
                try:
                    async for chunk in stream_openai_to_anthropic(
                        result['stream'],
                        model_id,
                        {},
                        input_tokens=input_tok_est,
                        expect_thinking=expect_thinking,
                        start_ms=result.get('start_ms', time.time() * 1000),
                    ):
                        yield chunk
                except Exception as e:
                    logger.error(f'[anthropic_stream] error: {e}')
                    # Best-effort terminal event so clients don't hang mid-turn
                    try:
                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                    except Exception:
                        pass
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
            return JSONResponse(status_code=status_code, content=anthropic_error(err.get('type', 'api_error'), err.get('message', 'Unknown error')))

        anthropic_resp = openai_to_anthropic(data, model_id, f"msg_{int(time.time())}", expect_thinking=expect_thinking, estimated_input=input_tok_est)
        return JSONResponse(status_code=200, content=anthropic_resp)

    async def proxy_openai(self, body: dict, req_headers: dict, model: str, req: Request = None, metric_path: str = '/v1/chat/completions'):
        sanitize_nvidia_payload(body)
        model_id = body.get('model') or model or ''
        if is_model_unavailable(model_id):
            return {'status': 404, 'data': {'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}}}

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return stream_guard

        # Transparent contract: exactly one requested model.  Retries below
        # rotate credentials only; they never construct model candidates.
        primary_body = json.loads(json.dumps(body))
        cand_model = model_id
        body = json.loads(json.dumps(primary_body))
        body['model'] = cand_model
        model_id = cand_model

        if body.get('max_completion_tokens') is not None and body.get('max_tokens') is None:
            body['max_tokens'] = body['max_completion_tokens']
            del body['max_completion_tokens']

        clamp_max_tokens_for_model(body, model_id)

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
                        self._record_model_response(model_id, key, resp.status, body_text, metric_path)
                        await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                        if self.metrics:
                            await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                        attempt += 1
                        continue
                    if resp.status >= 400:
                        resp_body = await _safe_response_body(resp)
                        self._record_model_response(model_id, key, resp.status, resp_body, metric_path)
                        classification = classify_upstream_error(resp.status, resp_body)
                        norm_status, resp_body = _normalize_upstream_error(resp.status, resp_body, model_id)
                        self._in_flight = max(0, self._in_flight - 1)
                        key.decrement_in_flight()
                        # Retry only failures that may change with time/key.
                        # Account-scoped deployment, capability and route
                        # errors must not be retried across identical keys.
                        retryable = classification['state'] in ('rate_limited', 'transient_failure', 'account_forbidden')
                        if retryable and attempt < max_attempts - 1:
                            attempt += 1
                            continue
                        return {'status': norm_status, 'data': resp_body}

                    # Keep in-flight until the stream consumer finishes. The wrapper
                    # owns release/decrement so every streaming surface (/chat,
                    # /messages, /responses) closes capacity exactly once.
                    released = False

                    async def stream_wrapper(resp=resp, key=key):
                        nonlocal released
                        try:
                            async for chunk, _ in resp.content.iter_chunks():
                                yield chunk
                        finally:
                            if not released:
                                released = True
                                try:
                                    resp.release()
                                except Exception:
                                    pass
                                self._in_flight = max(0, self._in_flight - 1)
                                try:
                                    key.decrement_in_flight()
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
                        self._record_model_response(model_id, key, resp.status, body_text, metric_path)
                        await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                        if self.metrics:
                            await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                        attempt += 1
                        continue

                    resp_data = await _safe_response_body(resp)
                    classification = classify_upstream_error(resp.status, resp_data)
                    if resp.status >= 400:
                        self._record_model_response(model_id, key, resp.status, resp_data, metric_path)
                    norm_status, resp_data = _normalize_upstream_error(resp.status, resp_data, model_id)
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()

                    if norm_status >= 400:
                        retryable = classification['state'] in ('rate_limited', 'transient_failure', 'account_forbidden')
                        if retryable and attempt < max_attempts - 1:
                            attempt += 1
                            continue
                        return {'status': norm_status, 'data': resp_data}

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
                    self._record_model_response(model_id, key, resp.status, body_text, path)
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                    attempt += 1
                    continue

                if is_streaming and resp.status < 400:
                    if self.metrics:
                        await self.metrics.record_request(
                            model=model_id, key_label=key.label,
                            status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                            path=path,
                        )
                    return StreamingResponse(
                        self._stream_proxy(resp, key),
                        media_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
                    )

                resp_data = await resp.read()
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()

                if resp.status >= 400:
                    try:
                        err_data = json.loads(resp_data)
                    except (json.JSONDecodeError, ValueError):
                        err_data = {'error': {'message': resp_data.decode('utf-8', errors='replace'), 'type': 'api_error'}}
                    self._record_model_response(model_id, key, resp.status, err_data, path)
                    classification = classify_upstream_error(resp.status, err_data)
                    retryable = classification['state'] in ('rate_limited', 'transient_failure', 'account_forbidden')
                    if retryable and attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    return JSONResponse(status_code=resp.status, content=err_data)

                if self.metrics:
                    await self.metrics.record_request(
                        model=model_id, key_label=key.label,
                        status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                        path=path,
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

        # D11: return 404 for paths that don't match any known API endpoint
        known_stems = ('/v1/chat/completions', '/v1/completions', '/v1/embeddings',
                       '/v1/models', '/v1/engines', '/v1/images', '/v1/audio',
                       '/v1/moderations', '/v1/responses', '/v1/files',
                       '/v1/fine_tuning', '/v1/batches', '/v1/ranking', '/v1/infer',
                       '/v1/messages', '/v1/messages/count_tokens',
                       '/v1/capabilities', '/v1/capabilities/params',
                       '/v2/', '/api/', '/v1/complete')
        normalized = path if path.startswith('/') else '/' + path
        if path != '/' and not any(normalized == s.rstrip('/') or normalized.startswith(s) for s in known_stems):
            return JSONResponse(status_code=404, content={'error': {'message': f'Unknown endpoint: {path}', 'type': 'invalid_request_error'}})

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
                    self._record_model_response(model_id, key, resp.status, body_text, path)
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        await self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra)
                    attempt += 1
                    continue

                content_type = resp.headers.get('content-type', '')
                if ('text/event-stream' in content_type or is_streaming) and resp.status < 400:
                    if self.metrics:
                        await self.metrics.record_request(
                            model=model_id, key_label=key.label,
                            status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                            path=path,
                        )
                    return StreamingResponse(
                        self._stream_proxy(resp, key),
                        media_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
                    )

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
    result['catalog_listed'] = True
    result['last_status'] = st.get('last_status', 0)
    result['ok'] = st.get('ok', True)
    result['reason'] = st.get('reason', '')
    result['reason_code'] = st.get('reason_code', '')
    result['verified'] = st.get('verified', False)
    result['availability_state'] = st.get('availability_state', 'unknown')
    result['availability_scope'] = st.get('availability_scope', 'account')
    result['checked_at'] = st.get('checked_at')
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
    # P3 CORS: reflective allow for localhost/127.0.0.1 (any port) so browser SDKs
    # (OpenAI/Anthropic/Codex) preflight works, while blocking non-local origins.
    allowed_cors_hosts = {'127.0.0.1', 'localhost', '::1'}

    def _cors_origin(origin: str) -> str:
        if not origin:
            return ''
        try:
            from urllib.parse import urlparse
            host = urlparse(origin).hostname or ''
        except Exception:
            return ''
        if host in allowed_cors_hosts:
            return origin
        return ''

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
        allow_methods=['*'],
        allow_headers=['*'],
        expose_headers=['*'],
        allow_credentials=True,
    )

    server = Server(app)
    server._register_routes()

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.exception(f"[global] Unhandled exception on {request.method} {request.url.path}: {exc}")
        return JSONResponse(status_code=500, content={'error': {'message': 'Internal server error', 'type': 'server_error'}})

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
