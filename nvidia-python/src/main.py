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
import hmac
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

try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../nvidia-python only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter, sanitize_header_value
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

    def sanitize_header_value(value):
        # Fallback sanitizer: upstream common.middleware is missing from the
        # repo, so provide the BUG-SEC2 header-injection guard inline.
        # Strip control chars that could be used for header injection (CRLF etc.)
        if not isinstance(value, str):
            value = str(value)
        return re_module.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()
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
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import (
        ModelStateStore,
        classify_upstream_error,
        credential_fingerprint,
        error_text,
    )
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id

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
_verify_cursor: int = 0

async def probe_model(pool, model_id: str, timeout_ms: int = 120000, key=None) -> dict:
    """Probe one model for one credential/account scope."""
    try:
        key = key or pool.peek_key()
        if not key:
            return {"ok": False, "status": 0, "reason": "no_key", "account_scope": "unknown"}

        body = {"model": model_id, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1, "stream": False}
        headers = {"Authorization": f"Bearer {key.api_key}"}
        account_scope = credential_fingerprint(key.api_key)
        # V-13 fix: probes consume real upstream RPM — record the timestamp on
        # the key so the pool's rate view includes verification traffic.
        try:
            key.timestamps.append(time.time())
        except Exception:
            pass
        session = getattr(pool, "_agent", None)
        owns_session = session is None or session.closed
        if owns_session:
            session = aiohttp.ClientSession()
        try:
            async with session.post(
                f"{BASE_LLM}/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000),
            ) as resp:
                if 200 <= resp.status < 300:
                    return {"ok": True, "status": resp.status, "reason": "", "account_scope": account_scope}
                text = await resp.text()
                return {
                    "ok": False,
                    "status": resp.status,
                    "reason": text[:4000],
                    "account_scope": account_scope,
                }
        finally:
            if owns_session:
                await session.close()
    except Exception as exc:
        return {"ok": False, "status": 0, "reason": str(exc)[:4000], "account_scope": "unknown"}


async def verify_models(pool):
    """Verify a rotating batch per configured credential scope.

    Verification is account-scoped. Only explicit provider EOL can affect the
    global retired set; all other outcomes remain observations.
    """
    global _unavailable_models, _retired_models, _model_status, _model_state_store, _verify_cursor
    ids = await pool.refresh_models(force=True) or []
    if not ids:
        return

    if _model_state_store:
        metadata = getattr(pool, "models_metadata", {}) or {}
        catalog_entries = [metadata.get(mid) or {"id": mid} for mid in ids]
        _model_state_store.upsert_catalog(catalog_entries, source="nvidia:/v1/models")
        MODEL_REGISTRY.register_catalog(catalog_entries, revision="runtime-catalog")
        MODEL_REGISTRY_CLIENT.schedule_catalog("nvidia", catalog_entries, "runtime-catalog")

    # Cover the whole catalog over successive sweeps instead of permanently
    # limiting verification to the first alphabetic 100 models.
    batch_size = min(100, len(ids))
    start = _verify_cursor % len(ids)
    probe_ids = [ids[(start + offset) % len(ids)] for offset in range(batch_size)]
    _verify_cursor = (start + batch_size) % len(ids)

    # Probe one representative key per distinct credential fingerprint. This
    # avoids claiming that one account's deployment state applies to another.
    probe_keys = []
    seen_scopes = set()
    for key in getattr(pool, "keys", []) or []:
        scope = credential_fingerprint(getattr(key, "api_key", None))
        if scope not in seen_scopes:
            seen_scopes.add(scope)
            probe_keys.append(key)
    if not probe_keys:
        peek_key = getattr(pool, "peek_key", None)
        key = peek_key() if callable(peek_key) else None
        if key:
            probe_keys = [key]
    if not probe_keys:
        probe_keys = [None]

    sem = asyncio.Semaphore(VERIFY_CONCURRENCY)
    observations: dict[str, list[dict]] = {}

    async def _probe(mid, key):
        async with sem:
            res = await probe_model(pool, mid, TTFT_TIMEOUT_MS, key=key)
            res["ts"] = time.time()  # V-20 fix: track probe recency
            classification = classify_upstream_error(res.get("status", 0), res.get("reason", ""))
            res["state"] = classification["state"]
            res["reason_code"] = classification["reason_code"]
            observations.setdefault(mid, []).append(res)

            if res["state"] == "globally_retired":
                _retired_models.add(mid)
            elif res.get("ok"):
                _retired_models.discard(mid)

            if _model_state_store:
                stored = await _model_state_store.record_status_async(
                    model_id=mid,
                    account_scope=res.get("account_scope", "unknown"),
                    state=res["state"] if not res.get("ok") else "available",
                    status_code=res.get("status", 0),
                    reason_code=res.get("reason_code", "OK" if res.get("ok") else ""),
                    reason_detail=res.get("reason", ""),
                    endpoint="/v1/chat/completions",
                )
                MODEL_REGISTRY_CLIENT.schedule_observation(
                    "nvidia", mid, stored.get("account_scope", "unknown"),
                    stored.get("state", res["state"]), res.get("status", 0),
                    stored.get("reason_code", ""), stored.get("reason_detail", ""),
                    "/v1/chat/completions",
                )

    await asyncio.gather(*[_probe(mid, key) for mid in probe_ids for key in probe_keys])

    # In-memory unavailable state is only a conservative aggregate for legacy
    # metrics. It is never a global retirement decision.
    for mid, results in observations.items():
        if results and all(not result.get("ok") for result in results):
            _unavailable_models.add(mid)
        else:
            _unavailable_models.discard(mid)
        # V-20 fix: "latest" means most recent probe, not alphabetical fingerprint.
        latest = max(results, key=lambda result: result.get("ts", 0))
        _model_status[mid] = latest

    _retired_models.intersection_update(set(ids))
    logger.info(
        f"[verify] sweep batch={len(probe_ids)} accounts={len(probe_keys)} "
        f"unavailable={len(_unavailable_models)} retired={len(_retired_models)}"
    )

async def verify_loop(pool):
    while True:
        try:
            # F7 fix: never compete with live traffic — skip the sweep while
            # requests are queued/paced on the key pool.
            if getattr(pool, '_waiting', None):
                logger.info('[verify] sweep skipped: %d live request(s) waiting on key pool'
                            % len(pool._waiting))
                await asyncio.sleep(30)
                continue
            await verify_models(pool)
            await asyncio.sleep(VERIFY_INTERVAL / 1000)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[verify] loop error: {e}")
            await asyncio.sleep(60)


# ----------------------------------------------------------------------
# .env HOT RELOAD WATCHER (full Node parity)
# ----------------------------------------------------------------------

def validate_config():
    """Validate required configuration at startup."""
    import os
    import sys
    
    missing = []
    for var in ['NVIDIA_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    # Validate port range
    try:
        port = int(os.environ.get('LISTEN_PORT', '9101'))
        if not (1024 <= port <= 65535):
            print(f"❌ ERROR: Invalid port {port}")
            sys.exit(1)
    except ValueError:
        print(f"❌ ERROR: LISTEN_PORT must be an integer")
        sys.exit(1)
    
    print(f"✅ Configuration validated successfully")


# Callbacks invoked after .env hot-reload. The wrapper server registers
# a pool-sync callback here during startup so new API keys take effect
# without a process restart.
_ENV_RELOAD_CALLBACKS = []


def start_env_watcher():
    """Start .env hot-reload watcher (exact parity with Node reloadDotenv + fs.watch).

    On .env modification, reloads env vars AND triggers the registered
    _ENV_RELOAD_CALLBACKS so the key pool can pick up new keys without
    a process restart.
    """
    if not HAS_WATCHDOG:
        logger.warning('[env] watchdog not available; hot reload disabled')
        return
    try:
        class EnvWatcher(FileSystemEventHandler):
            def on_modified(self, event):
                if event.src_path.endswith('.env') or event.src_path.endswith('/.env'):
                    load_dotenv(override=True)
                    # Invoke registered reload callbacks (e.g. pool.sync_from_env).
                    for cb in _ENV_RELOAD_CALLBACKS:
                        try:
                            cb()
                        except Exception as e:
                            logger.warning(f'[env] reload callback failed: {e}')
                    logger.info('[env] .env reloaded (hot)')

        observer = Observer()
        watch_path = str(Path(__file__).parent.parent)
        observer.schedule(EnvWatcher(), path=watch_path, recursive=False)
        observer.start()
        logger.info('[env] Watching .env for hot reload')
    except Exception as e:
        logger.warning(f'[env] Failed to start watcher: {e}')

from .key_pool import KeyPool, NVIDIA_BASE_URL, NVIDIA_GENAI_URL, NVIDIA_NVCF_URL

# ── Per-IP Rate Limiting ──
from collections import defaultdict
_rate_limit_store = defaultdict(list)
_rate_limit_lock = threading.Lock()
# Default 600 RPM per IP — high enough to support 4-5 concurrent agents on
# the same loopback IP without false-positive 429s. Operators can lower
# via env var, or set to 0 to disable per-IP limiting entirely.
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "600"))

def check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited.
    RATE_LIMIT_RPM=0 disables per-IP limiting entirely (multi-agent setups
    that prefer per-key RPM limits only)."""
    if RATE_LIMIT_RPM <= 0:
        return True
    now = time.time()
    with _rate_limit_lock:
        # V-08 fix: prune stale keys so the store cannot grow without bound.
        if len(_rate_limit_store) > 1024:
            for ip in list(_rate_limit_store.keys()):
                if not any(now - t < 60 for t in _rate_limit_store[ip]):
                    del _rate_limit_store[ip]
        fresh = [t for t in _rate_limit_store[client_ip] if now - t < 60]
        if len(fresh) >= RATE_LIMIT_RPM:
            _rate_limit_store[client_ip] = fresh
            return False
        fresh.append(now)
        _rate_limit_store[client_ip] = fresh
    return True



# Shared translation utilities from common/translations (deduplication).
try:
    from common.translations import (
        AnthropicStreamState as _SharedAnthropicStreamState,
        parse_dsml_from_text as _shared_parse_dsml,
        build_forward_headers as _build_forward_headers,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError:
    _USING_SHARED_TRANSLATIONS = False
    # Fallback: minimal build_forward_headers so the wrapper still works
    # if common.translations is not importable (shouldn't happen in prod).
    def _build_forward_headers(client_headers, extra=None):
        out = {}
        if client_headers and hasattr(client_headers, 'get'):
            for h in ('user-agent', 'anthropic-version', 'anthropic-beta',
                      'openai-beta', 'x-request-id', 'accept'):
                v = client_headers.get(h)
                if v:
                    out[h] = str(v)
        if extra:
            out.update(extra)
        return out

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

if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
    load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/nvidia-python/nvidia_py.log')
try:
    os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
    _log_file_handler = logging.FileHandler(LOG_FILE)
except Exception:
    LOG_FILE = '/tmp/wrapper-nvidia-python.log'
    _log_file_handler = logging.FileHandler(LOG_FILE)
logger = logging.getLogger('wrapper-nvidia')


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter (P2: enabled via WRAPPER_JSON_LOG=true)."""
    def format(self, record):
        return json.dumps({
            'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(record.created)),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }, ensure_ascii=False)


if os.environ.get('WRAPPER_JSON_LOG', '').lower() in ('1', 'true', 'yes'):
    _log_format = JsonFormatter()
    _log_file_handler.setFormatter(_log_format)
    logging.basicConfig(
        level=logging.INFO,
        handlers=[_log_file_handler, logging.StreamHandler(sys.stdout)],
    )
    for h in logging.root.handlers:
        h.setFormatter(_log_format)
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(message)s',
        handlers=[
            _log_file_handler,
            logging.StreamHandler(sys.stdout),
        ],
    )

# ---------------------------------------------------------------------------
# WRAPPER BEHAVIOR ENV FLAGS (audit fixes 2026-07-27) — all documented in
# nvidia-python/.env.example as well. Transparent-by-default: mutation happens
# only when compat/emulation strictly requires it or when explicitly opted in.
#
#   WRAPPER_AUTO_REASONING=0|1      (default 0) V1/F6: when 1, auto-inject default
#                                   reasoning params (chat_template_kwargs etc.) for
#                                   requires_reasoning models when the client did not
#                                   ask. Default OFF: no injection without client intent.
#   WRAPPER_SURFACE_REASONING=0|1   (default 0) V15: when 1, an empty message.content
#                                   on plain /v1/chat/completions is replaced with the
#                                   model's reasoning text. Default OFF: only the
#                                   SDK-safety null->"" fix is applied.
#   WRAPPER_DROP_BUILTIN_PARAMS=0|1 (default 1) V4: when 1, drop the built-in list of
#                                   params NVIDIA verifiably rejects (think, context_*
#                                   family). Set 0 to disable built-in drops; DROP_PARAMS
#                                   env keys are always dropped. max_output_tokens is
#                                   never silently deleted: it maps to max_tokens when
#                                   max_tokens is absent, else dropped with a debug log.
#   WRAPPER_FORCE_USAGE=0|1         (default 0) V7: when 1, inject
#                                   stream_options.include_usage on plain
#                                   /v1/chat/completions streams too. By default the
#                                   wrapper injects it only on translated /v1/messages
#                                   and /v1/responses paths (it needs usage there).
#   WRAPPER_AUTO_TRUNCATE=0|1       (default 0) V19: when 1, /v1/messages histories that
#                                   exceed the estimated context window are silently
#                                   truncated (oldest first). Default OFF: forward as-is
#                                   and let upstream decide.
#   WRAPPER_SYNTHETIC_THINKING=0|1  (default 0) V25: when 1, emit the synthetic
#                                   "[Reasoning not supported...]" thinking block when a
#                                   client requested thinking but the model produced
#                                   none. Default OFF: the thinking block is omitted.
#   STREAM_SOCK_READ_TIMEOUT_SEC    (default 300) V-09: read-idle timeout for streamed
#                                   upstream responses (replaces the hard total timeout
#                                   that killed long generations mid-stream).
#   LOKI_MAX_BUFFER / LOKI_AUTH_FAILURE_LIMIT  V-04: see loki_push.py.
#   WRAPPER_EVENTS_MAX_MB           (default 64) V-11: rotate wrapper-events.jsonl when
#                                   it exceeds this size.
#   RESPONSES_STORE_MAX_BYTES       (default 64MiB) V-22: byte cap for the /v1/responses
#                                   previous_response_id history store.
#   VERIFY_INTERVAL                 (default now 1800s, was 600) F7: verify sweeps also
#                                   skip while live requests are queued on the key pool.
# ---------------------------------------------------------------------------

def _env_flag(name: str, default: str = '0') -> bool:
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9101'))
# V-06 fix: default bind is loopback; set LISTEN_HOST explicitly to expose.
BIND_HOST = os.environ.get('LISTEN_HOST', '127.0.0.1')
BASE_LLM = (os.environ.get('NVIDIA_BASE_URL') or NVIDIA_BASE_URL).rstrip('/')
BASE_GENAI = (os.environ.get('NVIDIA_GENAI_URL') or NVIDIA_GENAI_URL).rstrip('/')
BASE_NVCF = (os.environ.get('NVIDIA_NVCF_URL') or NVIDIA_NVCF_URL).rstrip('/')
DB_PATH = os.environ.get('METRICS_DB', str(Path(__file__).parent.parent / 'metrics.db'))
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).resolve().parent.parent / 'model-state.db'))
MODEL_REGISTRY = LocalModelRegistry('nvidia', profile_db_path=MODEL_STATE_DB)
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
MAX_RETRIES = int(os.environ.get('QUIET_RETRIED_429', '3'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
HEADERS_TIMEOUT_MS = int(os.environ.get('HEADERS_TIMEOUT_MS', '120000'))
PRE_RESPONSE_TIMEOUT_MS = int(os.environ.get('PRE_RESPONSE_TIMEOUT_MS', '300000'))
TTFT_TIMEOUT_MS = int(os.environ.get('TTFT_TIMEOUT_MS', '120000'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT', '120'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '600'))
# V-09 fix: streams use a read-idle timeout instead of a hard total timeout.
STREAM_SOCK_READ_TIMEOUT_SEC = int(os.environ.get('STREAM_SOCK_READ_TIMEOUT_SEC', '300'))
GEN_TIMEOUT_SEC = int(os.environ.get('GEN_TIMEOUT_SEC', '900'))
ANTI_SILENCE_TIMEOUT_MS = int(os.environ.get('ANTI_SILENCE_TIMEOUT_MS', '960000'))
# Default OFF for multi-agent localhost deployments; per-key RPM limits
# already protect the upstream. Bump cap to 500 for headroom when enabled.
INFLIGHT_SOFT_CAP = int(os.environ.get('INFLIGHT_SOFT_CAP', '500'))
LOAD_SHEDDING_ENABLED = os.environ.get('LOAD_SHEDDING_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
VERIFY_CONCURRENCY = int(os.environ.get('VERIFY_CONCURRENCY', '8'))
# F7 fix: default sweep cadence lowered (600s -> 1800s); env still overrides.
VERIFY_INTERVAL = int(os.environ.get('VERIFY_INTERVAL', '1800')) * 1000
# V-13 fix: parse as a real boolean ('no'/'0'/'off' previously enabled it).
VERIFY_ON_BOOT = _env_flag('VERIFY_ON_BOOT', 'false')
MODEL_REFRESH_SEC = int(os.environ.get('MODEL_REFRESH_SEC', '600'))
MAX_STREAM_BUFFER_KB = int(os.environ.get('MAX_STREAM_BUFFER_KB', '512'))
MAX_STREAM_BUFFER = MAX_STREAM_BUFFER_KB * 1024
BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()
try:
    import importlib.metadata
    VERSION = f"{importlib.metadata.version('wrapper-nvidia')}-py"
except Exception:
    VERSION = '8.6.5-py'

# Build identity (H-04/H-02): resolve git root + source root from __file__, portable
def _resolve_git_root():
    try:
        import subprocess
        return subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=os.path.dirname(os.path.abspath(__file__)), stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        # fallback: walk up to find .git
        p = os.path.dirname(os.path.abspath(__file__))
        while p and p != os.path.dirname(p):
            if os.path.isdir(os.path.join(p, '.git')):
                return p
            p = os.path.dirname(p)
        return '/root/wrapper'

def _resolve_git_commit():
    try:
        import subprocess
        return subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'],
            cwd=_resolve_git_root(), stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return 'unknown'

GIT_COMMIT = _resolve_git_commit()
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../nvidia-python (up from src/)

REASONING_CONFIGS = [
    {'patterns': ['deepseek-v4', 'deepseek-r1', 'deepseek-reasoner'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True, 'thinking': True}, 'requires_reasoning': True},
    {'patterns': ['deepseek-coder'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    {'patterns': ['-reasoning', 'reason'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True, 'thinking': True}, 'requires_reasoning': True},
    {'patterns': ['thinkingmachines', 'inkling'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False},
    # BUG-M3 fix: exclude non-chat qwen models (image, VL) from reasoning injection.
    # find_reasoning_config matches by longest pattern; the exclude patterns below are
    # checked after the match to skip non-chat models.
    {'patterns': ['qwen'], 'mechanism': 'chat_template_kwargs', 'params': {'enable_thinking': True}, 'requires_reasoning': False, 'exclude': ['qwen-image', 'qwen-vl', 'qwen2-vl']},
    # GLM: do NOT auto-inject thinking. GLM reasoning adds 4-5s latency and
    # the client/curl default is no-thinking (fast). Only enable thinking when
    # the client explicitly requests it (Anthropic thinking:enabled path).
    # This keeps wrapper latency on par with direct curl for GLM.
    {'patterns': ['glm'], 'mechanism': 'chat_template_kwargs', 'params': {'thinking': False}, 'requires_reasoning': False, 'opt_out_default_thinking': True},
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
# V4 fix (transparency audit 2026-07-27):
#  - DROP_PARAMS env keys are always dropped (explicit operator opt-in).
#  - The built-in list is limited to keys NVIDIA verifiably rejects
#    (think + the context_* metadata family) and can be disabled entirely
#    with WRAPPER_DROP_BUILTIN_PARAMS=0.
#  - max_output_tokens is NOT silently deleted anymore; see
#    sanitize_nvidia_payload() which maps it to max_tokens when absent.
_BUILTIN_DROP_PARAMS = {
    'think', 'context_length', 'context_window', 'context_len',
    'max_position_embeddings', 'max_context_length', 'max_input_tokens',
    'token_limit',
}
PROACTIVE_DROP = set(p.strip() for p in (os.environ.get('DROP_PARAMS') or '').split(',') if p.strip())
if _env_flag('WRAPPER_DROP_BUILTIN_PARAMS', '1'):
    PROACTIVE_DROP.update(_BUILTIN_DROP_PARAMS)
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
            # BUG-M3 fix: skip configs whose exclude patterns match this model
            # (e.g. qwen-image, qwen-vl should not receive reasoning params)
            excludes = cfg.get('exclude') or []
            if any(exc in m for exc in excludes):
                continue
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
            # V3 fix (GLM regression): honor client intent. When the client
            # explicitly requests thinking, config default-off values (e.g.
            # GLM's {'thinking': False} opt-out) must not force it back off.
            # Opting out of default thinking applies only when the client did
            # NOT request thinking (handled by the `enabled` branch below).
            obj[k] = (True if v is False else v) if enabled else False
        oai_body['chat_template_kwargs'] = {**(oai_body.get('chat_template_kwargs') or {}), **obj}
    elif cfg['mechanism'] == 'reasoning_effort':
        oai_body['reasoning_effort'] = cfg['params'].get('effort', 'high') if enabled else 'low'
    elif cfg['mechanism'] == 'nemotron_chat_template':
        obj = {}
        for k, v in cfg['params'].items():
            obj[k] = (True if v is False else v) if enabled else False
        oai_body['chat_template_kwargs'] = {**(oai_body.get('chat_template_kwargs') or {}), **obj}
        rb = (oai_body.get('extra_body', {}).get('reasoning_budget') if isinstance(oai_body.get('extra_body'), dict) else None) or \
             (oai_body.get('chat_template_kwargs', {}).get('reasoning_budget') if isinstance(oai_body.get('chat_template_kwargs'), dict) else None)
        if rb is not None:
            oai_body['extra_body'] = {**(oai_body.get('extra_body') or {}), 'reasoning_budget': rb}


def apply_default_reasoning(body: dict, model_id: str) -> None:
    # V1/F6 fix (transparency audit 2026-07-27): auto-injection of reasoning
    # params when the client did not ask is opt-in via WRAPPER_AUTO_REASONING.
    if not _env_flag('WRAPPER_AUTO_REASONING', '0'):
        return
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
        # BUG-H2 fix: require exact match or hyphen-separated variant match.
        # Dot-separated extensions are version numbers (e.g. glm-5.3 is newer
        # than deprecated glm-5); they must NOT redirect to an older target.
        if stem and got and got != cur.split('/')[1]:
            if got == stem or got.startswith(stem + '-'):
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
    """Preserve provider model IDs exactly; context annotations are adapter-specific.

    This function remains for compatibility with callers but must not mutate a
    concrete model identity in the transparent wrapper.
    """
    return model_id

ALIAS_TO_NIM = {}  # kept for metrics/debug: maps alias -> last dynamic target (informational)
DISCOVERY_TO_NIM = {}
DISCOVERY_PREFIX = 'claude-'

# Canonical Claude Code / Anthropic short names — NEVER hardcode a backing model.
# They resolve only from an explicit operator/scoped binding; concrete requests
# never mutate alias state.
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
_dynamic_alias_target: str = ''  # operator-configured via DYNAMIC_ALIAS_TARGET env var at startup
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
        MODEL_REGISTRY.bind_explicit_aliases(seed, _ALIAS_NAME_SET, scope_type="wrapper", scope_id="nvidia")
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
            # V8 fix: clamping stays (upstream would 400) but must be observable.
            logger.warning(f'[model-cap] clamping {key} for {model_id}: {val} -> {cap}')
            body[key] = cap


def route_upstream(path: str) -> str:
    if path.startswith('/v1/images') or path.startswith('/v1/audio') or path.startswith('/v1/video') or path.startswith('/v1/ranking') or path.startswith('/v1/infer'):
        return BASE_GENAI
    return BASE_LLM


def model_from_path(path: str) -> str:
    """Extract model ID from a URL path, preserving org/model-name structure.

    BUG-D7 fix: the original implementation returned only the last path segment,
    which dropped the org prefix (e.g., /v1/models/meta/llama-3.1-8b-instruct
    returned 'llama-3.1-8b-instruct' instead of 'meta/llama-3.1-8b-instruct').

    Known path patterns with model at end:
      /v1/models/<org>/<model>       -> org/model
      /v1/models/<model>             -> model
      /v1/engines/<org>/<model>      -> org/model
      /chat/completions              -> '' (model in body)
    """
    parts = path.strip('/').split('/')
    # Find the model start: after /v1/models/ or /v1/engines/
    for i, part in enumerate(parts):
        if part in ('models', 'engines') and i + 1 < len(parts):
            return '/'.join(parts[i + 1:])
    if len(parts) >= 2:
        return parts[-1]
    return ''


def forward_headers(request: Request) -> dict:
    """Transparent proxy: forward ALL client headers via shared build_forward_headers.

    Per project principle #1: wrappers must NOT drop client headers. Only
    swap Authorization (done by caller) and set Content-Type. Everything
    else (user-agent, x-stainless-*, anthropic-*, openai-*, x-request-id,
    x-correlation-id, accept, accept-language, etc.) is forwarded verbatim.
    """
    headers = _build_forward_headers(request.headers)
    # Generate a request ID if the client didn't provide one
    if 'x-request-id' not in headers:
        headers['x-request-id'] = generate_request_id()
    return headers


def client_ip(request: Request) -> str:
    # V-08 fix: rate limiting must key on the socket peer address, not the
    # client-controlled x-forwarded-for/x-real-ip headers (spoof/bypass/poison).
    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


def generate_request_id() -> str:
    return f"req_{int(time.time())}_{uuid.uuid4().hex[:8]}"


def add_rate_limit_headers(resp_headers: dict, key_label: str) -> None:
    pass


def sanitize_nvidia_payload(body: dict) -> None:
    # V4 fix: NVIDIA rejects max_output_tokens, but it carries client intent —
    # map it to max_tokens when max_tokens is absent instead of deleting it.
    if isinstance(body, dict) and 'max_output_tokens' in body:
        mot = body.pop('max_output_tokens')
        if body.get('max_tokens') is None:
            try:
                body['max_tokens'] = int(mot)
            except (TypeError, ValueError):
                logger.debug(f'[sanitize] dropped non-integer max_output_tokens={mot!r}')
        else:
            logger.debug('[sanitize] dropped max_output_tokens (max_tokens already present)')
    for p in PROACTIVE_DROP:
        if p not in PROTECTED_PARAMS:
            body.pop(p, None)


def ensure_nonempty_content(data: dict) -> None:
    if data.get('choices') and len(data['choices']) > 0:
        msg = data['choices'][0].get('message', {})
        if not msg.get('content') and not msg.get('tool_calls'):
            nr = extract_internal_reasoning(msg)
            # V15 fix (transparency audit 2026-07-27): surfacing private
            # reasoning as the answer text is opt-in via
            # WRAPPER_SURFACE_REASONING. The null->"" SDK-safety fix stays
            # unconditional.
            if nr.get('reasoning') and _env_flag('WRAPPER_SURFACE_REASONING', '0'):
                msg['content'] = nr['reasoning']
            elif msg.get('content') is None or not msg.get('content'):
                msg['content'] = ''


def pre_response_timeout_ms_for(model_id: str) -> int:
    return PRE_RESPONSE_TIMEOUT_MS



def _parse_retry_after(value, default: int = 65) -> int:
    """V-12 fix: Retry-After may be an integer OR an RFC HTTP-date.

    The previous bare int() raised on date format, silently skipping 429
    registration (no cooldown) via the broad except around the request.
    """
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    try:
        return max(0, int(s))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            import datetime as _dt
            now = _dt.datetime.now(dt.tzinfo) if dt.tzinfo else _dt.datetime.utcnow()
            return max(0, int((dt - now).total_seconds()))
    except Exception:
        pass
    return default


# F2 round-2 fix: the event loop only keeps WEAK references to tasks, so a
# fire-and-forget task with no other reference can be garbage-collected
# mid-flight, silently dropping a pending metrics/DB write. Keep a strong
# reference here until the task completes.
_BG_TASKS: set = set()


def _fire_and_forget(coro, label: str = 'bg') -> None:
    """F2 fix (latency audit 2026-07-27): run metrics/DB writes off the hot
    path. The task is retained in _BG_TASKS so it cannot be GC'd mid-flight,
    and exceptions are logged instead of being silently dropped."""
    def _done(task: 'asyncio.Task'):
        _BG_TASKS.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.warning(f'[{label}] background task failed: {exc}')
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop (import-time/test context) — close the coroutine.
        coro.close()
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_done)


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
        self.model_registry_client = MODEL_REGISTRY_CLIENT
        _model_state_store = self.model_state
        self.metrics: Optional[Metrics] = None
        self.registry: Optional[Registry] = None
        self.responses_handler: Optional[ResponsesHandler] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._agent: Optional[aiohttp.TCPConnector] = None
        self._in_flight = 0
        self._sse_clients: set = set()
        self._start_time = time.time()
        self._bg_tasks: list = []  # V-17 fix: retained background task handles

    async def init(self):
        await MODEL_REGISTRY_CLIENT.start()
        self.pool.load_from_env()
        self._agent = aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS)
        self._session = aiohttp.ClientSession(connector=self._agent)
        self.pool.set_external_session(self._session)

        self.metrics = Metrics(DB_PATH)
        await self.metrics.init()

        # Default EVENTS_FILE inside the nvidia-python wrapper directory (not
        # the deprecated /nvidia/ sibling). Resolves to a path next to src/.
        _default_events_file = str(Path(__file__).resolve().parents[1] / 'metrics_data' / 'wrapper-events.jsonl')
        EVENTS_FILE = os.environ.get('EVENTS_FILE', _default_events_file)
        os.makedirs(os.path.dirname(EVENTS_FILE), exist_ok=True)
        events_max_bytes = int(os.environ.get('WRAPPER_EVENTS_MAX_MB', '64')) * 1024 * 1024

        def _write_event_sync(ev: dict):
            # V-11 fix: rotate the events file so it cannot grow without bound.
            try:
                try:
                    if os.path.getsize(EVENTS_FILE) > events_max_bytes:
                        os.replace(EVENTS_FILE, EVENTS_FILE + '.1')
                except OSError:
                    pass
                with open(EVENTS_FILE, 'a') as f:
                    f.write(json.dumps(ev) + '\n')
            except Exception:
                pass

        async def _write_event(ev: dict):
            # V-11 fix: blocking file I/O runs in a worker thread, not on the loop.
            await asyncio.to_thread(_write_event_sync, ev)

        # F2 round-2 fix: bare create_task kept no strong reference (loop holds
        # weak refs) — a pending event write could be GC'd mid-flight. Route
        # through _fire_and_forget which retains the task and logs failures.
        self.metrics.on_request(lambda ev: _fire_and_forget(_write_event(ev), 'event-write'))
        self.metrics.on_rate_limit(lambda ev: _fire_and_forget(_write_event(ev), 'event-write'))

        alert_history.SOURCE = EVENTS_FILE
        loki_push.SOURCE = EVENTS_FILE
        # V-17 fix: retain background task handles so shutdown can cancel them.
        self._bg_tasks.append(asyncio.create_task(alert_history.mode_daemon()))
        self._bg_tasks.append(asyncio.create_task(loki_push.daemon()))

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
                MODEL_REGISTRY_CLIENT.schedule_catalog('nvidia', [metadata.get(mid) or {"id": mid} for mid in ids], 'runtime-catalog')
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
            self._bg_tasks.append(asyncio.create_task(verify_models(self.pool)))
        self._bg_tasks.append(asyncio.create_task(verify_loop(self.pool)))

        # V-17 fix: schedule metrics DB pruning daily (was never scheduled).
        async def _metrics_prune_loop():
            while True:
                await asyncio.sleep(86400)
                try:
                    await self.metrics.prune()
                    logger.info('[metrics] daily prune completed')
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f'[metrics] prune failed: {e}')

        # V-18 fix: heal stuck in_flight counters periodically (Node parity),
        # not only via the manual admin endpoint.
        async def _heal_in_flight_loop():
            while True:
                await asyncio.sleep(300)
                try:
                    await self.pool.heal_in_flight()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f'[heal] heal_in_flight failed: {e}')

        self._bg_tasks.append(asyncio.create_task(_metrics_prune_loop()))
        self._bg_tasks.append(asyncio.create_task(_heal_in_flight_loop()))

        # Register a hot-reload callback so new NVIDIA_API_KEY_* entries
        # in .env are picked up without a process restart. sync_keys is
        # async, so we schedule it on the running event loop.
        def _sync_pool_from_env():
            import re as _re
            new_keys = []
            seen = set()
            for key_name, value in sorted(os.environ.items()):
                if _re.match(r'^NVIDIA_API_KEY(_\d+)?$', key_name):
                    v = (value or '').strip()
                    if v and len(v) >= 10 and v not in seen:
                        seen.add(v)
                        new_keys.append(v)
            if new_keys:
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(self.pool.sync_keys(new_keys), loop)
                    logger.info(f'[env] scheduled pool.sync_keys({len(new_keys)} keys)')
                except Exception as e:
                    logger.warning(f'[env] pool sync schedule failed: {e}')

        _ENV_RELOAD_CALLBACKS.append(_sync_pool_from_env)
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

    async def _record_model_response(self, model_id: str, key, status: int, payload: Any, endpoint: str):
        """Persist and asynchronously publish provider result, never raw credentials."""
        credential = getattr(key, 'api_key', None)
        try:
            result = await self.model_state.record_error_async(
                model_id=model_id,
                account_credential=credential,
                status_code=status,
                payload=payload,
                endpoint=endpoint,
            )
            self.model_registry_client.schedule_observation(
                'nvidia', model_id, result.get('account_scope', credential_fingerprint(credential)),
                result.get('state', 'unknown'), status, result.get('reason_code', ''),
                result.get('reason_detail', ''), endpoint,
            )
        except Exception as e:
            logger.warning(f'[model-state] response record failed: {e}')

    def _register_routes(self):
        app = self.app

        
        # Latency tracking middleware
        @app.middleware("http")
        async def add_latency_tracking(request: Request, call_next):
            import time
            import uuid
            start_time = time.time()
            request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
            
            response = await call_next(request)
            
            latency_ms = (time.time() - start_time) * 1000
            
            logger.info(
                f"[{app.title}] request_id={request_id} "
                f"method={request.method} path={request.url.path} "
                f"latency={latency_ms:.2f}ms status={response.status_code}"
            )
            
            response.headers["X-Process-Time"] = f"{latency_ms:.2f}ms"
            response.headers["X-Request-ID"] = request_id
            return response

        @app.middleware('http')
        async def auth_middleware(request: Request, call_next):
            path = request.url.path
            method = request.method
            # CORS preflight must pass without auth so browser SDKs work
            if method == 'OPTIONS':
                return await call_next(request)
            # Per project principle: dashboard + metrics + telemetry must be
            # fast/precise/accessible WITHOUT token. Security is NOT mandatory
            # for the dashboard or telemetry. Auth is only enforced on
            # inference endpoints (/v1/chat/completions, /v1/messages, etc.).
            public_paths = ['/health', '/ready', '/metrics', '/metrics/prom',
                            '/metrics/models', '/metrics/models/timeseries', '/metrics/keys',
                            '/metrics/activity', '/metrics/rate-limits', '/metrics/model-status',
                            '/metrics/chart', '/stats', '/events', '/version', '/api/version',
                            '/', '/favicon.ico', '/dashboard', '/dashboard.html']
            is_public = (path in public_paths
                         or path.startswith('/metrics/')
                         or path.startswith('/stats')
                         or (method == 'GET' and path == '/v1/models')
                         or (method == 'GET' and path.startswith('/v1/models/'))
                         or (method == 'GET' and path == '/v1/engines')
                         or (method == 'GET' and path.startswith('/v1/engines/'))
                         or (method == 'GET' and path in ('/version', '/api/version'))
                         or (method == 'GET' and path == '/api/tags')
                         or (method == 'GET' and path in ('/api/v1/models', '/models'))
                         or (method == 'GET' and path in ('/props', '/v1/props'))
                         or (method == 'GET' and path == '/v1/capabilities')
                         or (method == 'GET' and path == '/v1/capabilities/params')
                         or (method == 'GET' and path.startswith('/catalog/'))
                         or (method == 'GET' and path.startswith('/mcp/')))

            # Per-IP rate limiting
            _client_ip = client_ip(request)
            if not check_rate_limit(_client_ip):
                return JSONResponse(status_code=429, content={'error': {'message': 'Too many requests', 'type': 'rate_limit_error'}})

            if BEARER_TOKEN and not is_public and not os.environ.get('DISABLE_AUTH'):
                # V-19 fix: constant-time comparison; check authorization and
                # x-api-key independently (a garbage Authorization header must
                # not mask a valid x-api-key).
                auth_header = (request.headers.get('authorization') or '').strip()
                api_key_header = (request.headers.get('x-api-key') or '').strip()
                candidates = []
                if auth_header:
                    candidates.append(auth_header[7:].strip() if auth_header.lower().startswith('bearer ') else auth_header)
                if api_key_header:
                    candidates.append(api_key_header)
                authorized = any(hmac.compare_digest(t, BEARER_TOKEN) for t in candidates)
                if not authorized:
                    # D11: unknown paths return 404, not 401 (don't leak route info)
                    known_stems = ('/v1/chat/completions', '/v1/completions', '/v1/embeddings',
                                   '/v1/models', '/v1/engines', '/v1/images', '/v1/audio',
                                   '/v1/moderations', '/v1/responses', '/v1/files',
                                   '/v1/fine_tuning', '/v1/batches', '/v1/ranking', '/v1/infer',
                                   '/v1/messages', '/v1/messages/count_tokens',
                                   '/v1/capabilities', '/v1/capabilities/params',
                                   '/v2/', '/api/', '/v1/complete',
                                   '/dashboard', '/dashboard.html',
                                   '/metrics', '/metrics/prom', '/metrics/models',
                                   '/metrics/keys', '/metrics/activity', '/metrics/rate-limits',
                                   '/metrics/model-status', '/metrics/chart',
                                   '/stats', '/events', '/version', '/api/version',
                                   '/admin', '/catalog', '/mcp')
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
                'git_commit': GIT_COMMIT,
                'source_root': SOURCE_ROOT,
                'pid': os.getpid(),
                'keys': self.pool.total_keys,
                'available': self.pool.available_keys,
                'live_keys': self.pool.all_stats(),
                'models_cached': len(self.pool.models_cached),
                'model_registry': MODEL_REGISTRY_CLIENT.stats(),
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
            return {'version': VERSION, 'git_commit': GIT_COMMIT, 'source_root': SOURCE_ROOT, 'pid': os.getpid()}

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
            """Serve dashboard.html WITHOUT embedding any secret.

            V-06 round-2 fix (mirrors nous N-03): the bearer token is never
            injected into the HTML — a client that can reach the port must not
            be able to read the proxy credential out of the page. The route
            itself now requires auth (removed from public_paths), and the page
            obtains the token client-side via a sessionStorage prompt.
            """
            dashboard_path = Path(__file__).parent.parent / 'dashboard.html'
            if not dashboard_path.exists():
                return HTMLResponse(content='<html><body><h1>wrapper-nvidia</h1>'
                                            '<p>See /metrics, /metrics/prom, /v1/models</p></body></html>')
            return HTMLResponse(content=dashboard_path.read_text())

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

            # BUG-SEC3 fix: cap max_tokens to prevent integer overflow / abuse.
            # 1M tokens is far beyond any model's context window.
            _MAX_TOKENS_LIMIT = 1_000_000
            for _mt_key in ('max_tokens', 'max_completion_tokens'):
                _mt_val = body.get(_mt_key)
                if _mt_val is not None and isinstance(_mt_val, int) and _mt_val > _MAX_TOKENS_LIMIT:
                    return JSONResponse(status_code=400, content={'error': {
                        'message': f'{_mt_key} exceeds maximum allowed value of {_MAX_TOKENS_LIMIT}',
                        'type': 'invalid_request_error',
                    }})

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
            # V-14 fix: the recursive debug walk only runs when DEBUG logging is
            # actually enabled — no O(body) CPU on the Codex hot path otherwise.
            if logger.isEnabledFor(logging.DEBUG):
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
            # BUG-ROUTE1 fix: /v1/embeddings routes to BASE_GENAI, not BASE_LLM.
            # resolve_base(model_id) always returned BASE_LLM because a model_id
            # never starts with '/v1/embeddings'. Use route_upstream directly.
            return await self._proxy_post(request, body, raw, model_id, '/v1/embeddings', lambda key: f"{route_upstream('/v1/embeddings')}/v1/embeddings")

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
            # BUG-ROUTE1 fix: /v1/ranking must route to BASE_GENAI (image/audio/
            # video/ranking/infer family), not BASE_LLM. resolve_base(model_id)
            # always returned BASE_LLM because a model_id never starts with
            # '/v1/ranking'. Use the endpoint path for routing.
            return await self._proxy_post(request, body, raw, model_id, '/v1/ranking', lambda key: f"{route_upstream('/v1/ranking')}/v1/ranking")

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

        # BUG-SEC3 fix: cap max_tokens to prevent integer overflow / abuse
        if body['max_tokens'] > 1_000_000:
            return JSONResponse(status_code=400, content=anthropic_error('invalid_request_error', 'max_tokens exceeds maximum allowed value of 1000000'))

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

    def _select_timeout(self, is_streaming: bool, metric_path: str) -> int:
        """Select timeout based on request type (P2: extracted from proxy_openai)."""
        is_gen = bool(re_module.search(r'images|genai|infer|audio|video|ranking', metric_path or ''))
        if is_streaming:
            return max(STREAM_REQUEST_TIMEOUT_SEC, ANTI_SILENCE_TIMEOUT_MS // 1000)
        elif is_gen:
            return GEN_TIMEOUT_SEC
        return REQUEST_TIMEOUT_SEC

    def _client_timeout(self, is_streaming: bool, metric_path: str) -> 'aiohttp.ClientTimeout':
        """V-09 fix: streamed responses must not be killed by a hard total
        timeout mid-generation. Streams use a read-idle (sock_read) timeout so
        a dead upstream is detected quickly while long generations survive."""
        if is_streaming:
            return aiohttp.ClientTimeout(total=None, sock_connect=30,
                                         sock_read=STREAM_SOCK_READ_TIMEOUT_SEC)
        return aiohttp.ClientTimeout(total=self._select_timeout(False, metric_path))

    def _classify_retry(self, status: int, classification: dict) -> bool:
        """Determine if a failed request should be retried with another key.
        
        BUG-RETRY1 fix: use the actual ErrorState enum values from contracts.py.
        Previously checked for 'rate_limited' which never matched the real values
        'key_rate_limited' / 'model_rate_limited', so 429 responses were never
        retried across keys — defeating multi-key rotation entirely.
        """
        state = classification['state']
        # Prefer the explicit retry flag from the classification when available
        if classification['retry_same_model']:
            return True
        return state in (
            'key_rate_limited', 'model_rate_limited',
            'transient_failure', 'account_forbidden',
            'network_timeout',
        )

    async def _prepare_proxy_body(self, body: dict, model_id: str, metric_path: str = '/v1/chat/completions') -> dict:
        """Prepare request body for upstream (P2: extracted from proxy_openai)."""
        body = json.loads(json.dumps(body))  # deep copy
        # V6: map max_completion_tokens -> max_tokens only when max_tokens is
        # absent (NIM needs max_tokens); never delete it when max_tokens exists.
        if body.get('max_completion_tokens') is not None and body.get('max_tokens') is None:
            body['max_tokens'] = body['max_completion_tokens']
            del body['max_completion_tokens']
        clamp_max_tokens_for_model(body, model_id)
        # V7 fix: inject stream_options.include_usage only where the wrapper
        # itself needs usage (translated /v1/messages and /v1/responses paths),
        # or when the operator forces it via WRAPPER_FORCE_USAGE=1. Plain
        # /v1/chat/completions passthrough stays untouched.
        wrapper_needs_usage = metric_path in ('/v1/messages', '/v1/responses')
        if body.get('stream') and (wrapper_needs_usage or _env_flag('WRAPPER_FORCE_USAGE', '0')):
            body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage': True}
        return body

    async def proxy_openai(self, body: dict, req_headers: dict, model: str, req: Request = None, metric_path: str = '/v1/chat/completions'):
        sanitize_nvidia_payload(body)
        model_id = body.get('model') or model or ''
        if is_model_unavailable(model_id):
            return {'status': 404, 'data': {'error': {'message': f'Model {model_id} is retired or unavailable', 'type': 'invalid_request_error'}}}

        stream_guard = guard_stream_unsupported(body, model_id)
        if stream_guard:
            return stream_guard

        client_surface = 'anthropic_messages' if metric_path == '/v1/messages' else 'openai_chat'
        try:
            call_plan = MODEL_REGISTRY.call_plan(model_id, client_surface)
            if not same_provider_model_id('nvidia', call_plan.model.provider_model_id, model_id):
                return {'status': 500, 'data': {'error': {'message': 'Model identity changed during call-plan resolution', 'type': 'server_error', 'code': 'MODEL_ID_MUTATION'}}}
        except ValueError as exc:
            return {'status': 400, 'data': {'error': {'message': str(exc), 'type': 'invalid_request_error', 'code': 'MODEL_CALL_PLAN_INVALID'}}}

        # Transparent contract: exactly one requested model.  Retries below
        # rotate credentials only; they never construct model candidates.
        primary_body = await self._prepare_proxy_body(body, model_id, metric_path=metric_path)
        cand_model = model_id
        body = json.loads(json.dumps(primary_body))
        body['model'] = cand_model
        model_id = cand_model

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

        # BUG-ROUTE1 fix: route by endpoint path, not model_id. A model_id
        # never starts with '/v1/images' or '/v1/ranking', so the old code
        # always returned BASE_LLM. Using the call_plan.path correctly routes
        # chat→BASE_LLM and image/ranking/infer→BASE_GENAI.
        target_url = f"{route_upstream(call_plan.path)}{call_plan.path}"

        start_ms = time.time() * 1000
        attempt = 0
        max_attempts = max(MAX_RETRIES + 1, self.pool.total_keys)

        while attempt < max_attempts:
            key_result = await self.pool.acquire(model_id)
            if not key_result:
                # Return 429 (not 503) so client SDKs (Anthropic/OpenAI) auto-retry with backoff.
                # 503 is treated as non-retryable fatal server error by most SDKs.
                return {'status': 429, 'headers': {'Retry-After': '30'}, 'data': {'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}}}

            key = key_result['key']
            self._in_flight += 1

            try:
                fwd_headers = {
                    'Authorization': f'Bearer {key.api_key}',
                    **headers,
                }

                is_streaming = bool(body.get('stream'))
                req_timeout = self._client_timeout(is_streaming, metric_path)  # V-09 fix

                if body.get('stream'):
                    resp = await self._session.post(
                        target_url, json=body, headers=fwd_headers,
                        timeout=req_timeout,
                    )
                    if resp.status == 429:
                        ra = _parse_retry_after(resp.headers.get('retry-after'))  # V-12 fix
                        self._in_flight = max(0, self._in_flight - 1)
                        key.decrement_in_flight()
                        body_text = await resp.text()
                        await self._record_model_response(model_id, key, resp.status, body_text, metric_path)
                        await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                        if self.metrics:
                            # F2 fix: DB write off the hot path
                            _fire_and_forget(self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra), 'metrics')
                        attempt += 1
                        continue
                    if resp.status >= 400:
                        resp_body = await _safe_response_body(resp)
                        await self._record_model_response(model_id, key, resp.status, resp_body, metric_path)
                        classification = classify_upstream_error(resp.status, resp_body)
                        norm_status, resp_body = _normalize_upstream_error(resp.status, resp_body, model_id)
                        self._in_flight = max(0, self._in_flight - 1)
                        key.decrement_in_flight()
                        # Retry only failures that may change with time/key.
                        # Account-scoped deployment, capability and route
                        # errors must not be retried across identical keys.
                        retryable = self._classify_retry(resp.status, classification)
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
                        # Idle-aware heartbeat: uses sentinel-task pattern so
                        # heartbeat fires EVEN when upstream is silent (reasoning
                        # models thinking 30+ sec). Without this, client/LB idle
                        # timeouts kill the stream mid-turn → "response berhenti".
                        last_hb = time.time()
                        at_line_boundary = True
                        saw_done = False
                        hb_interval = float(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000')) / 1000.0
                        try:
                            aiter = resp.content.__aiter__()
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=hb_interval)
                                except asyncio.TimeoutError:
                                    # Upstream idle — inject heartbeat at line boundary
                                    if at_line_boundary:
                                        yield b': heartbeat\n\n'
                                        last_hb = time.time()
                                    continue
                                except StopAsyncIteration:
                                    break
                                # Check for [DONE] marker
                                if isinstance(chunk, (bytes, bytearray)):
                                    if b'data: [DONE]' in chunk or b'data:[DONE]' in chunk:
                                        saw_done = True
                                else:
                                    if 'data: [DONE]' in str(chunk) or 'data:[DONE]' in str(chunk):
                                        saw_done = True
                                yield chunk
                                if isinstance(chunk, (bytes, bytearray)) and len(chunk):
                                    at_line_boundary = chunk.endswith(b'\n')
                                elif chunk:
                                    at_line_boundary = str(chunk).endswith('\n')
                                last_hb = time.time()
                            # Synthesize [DONE] if upstream EOF'd without one
                            if not saw_done:
                                yield b'data: [DONE]\n\n'
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
                        timeout=req_timeout,
                    )

                    if resp.status == 429:
                        ra = _parse_retry_after(resp.headers.get('retry-after'))  # V-12 fix
                        self._in_flight = max(0, self._in_flight - 1)
                        key.decrement_in_flight()
                        body_text = await resp.text()
                        await self._record_model_response(model_id, key, resp.status, body_text, metric_path)
                        await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                        if self.metrics:
                            # F2 fix: DB write off the hot path
                            _fire_and_forget(self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra), 'metrics')
                        attempt += 1
                        continue

                    resp_data = await _safe_response_body(resp)
                    classification = classify_upstream_error(resp.status, resp_data)
                    if resp.status >= 400:
                        await self._record_model_response(model_id, key, resp.status, resp_data, metric_path)
                    norm_status, resp_data = _normalize_upstream_error(resp.status, resp_data, model_id)
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()

                    if norm_status >= 400:
                        retryable = self._classify_retry(resp.status, classification)
                        if retryable and attempt < max_attempts - 1:
                            attempt += 1
                            continue
                        return {'status': norm_status, 'data': resp_data}

                    if self.metrics:
                        # F2 fix: DB write off the hot path
                        _fire_and_forget(self.metrics.record_request(
                            model=model_id, key_label=key.label,
                            status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                            prompt_tokens=resp_data.get('usage', {}).get('prompt_tokens', 0),
                            completion_tokens=resp_data.get('usage', {}).get('completion_tokens', 0),
                            path=metric_path,
                        ), 'metrics')
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

        return {'status': 429, 'headers': {'Retry-After': '30'}, 'data': {'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}}}


    def _resolve_base(self, model_id: str) -> str:
        # BUG-ROUTE1: delegate to route_upstream with a synthetic path so
        # image/genai models still route to BASE_GENAI when called from
        # legacy paths that pass model_id directly.
        return route_upstream(model_id)

    async def _proxy_post(self, request: Request, body: dict, raw: bytes, model_id: str, path: str, get_target_url):
        attempt = 0
        max_attempts = max(MAX_RETRIES + 1, self.pool.total_keys)
        is_streaming = bool(body.get('stream'))

        while attempt < max_attempts:
            key_result = await self.pool.acquire(model_id)
            if not key_result:
                return JSONResponse(status_code=429, headers={'Retry-After': '30'}, content={'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}})

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

                # P2/V-09: streaming uses read-idle timeout instead of total
                req_timeout = self._client_timeout(is_streaming, path)

                resp = await self._session.post(
                    target_url, json=body, headers=fwd_headers,
                    timeout=req_timeout,
                )

                if resp.status == 429:
                    ra = _parse_retry_after(resp.headers.get('retry-after'))  # V-12 fix
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    body_text = await resp.text()
                    await self._record_model_response(model_id, key, resp.status, body_text, path)
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        # F2 fix: DB write off the hot path
                        _fire_and_forget(self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra), 'metrics')
                    attempt += 1
                    continue

                if is_streaming and resp.status < 400:
                    if self.metrics:
                        # F2 fix: DB write off the hot path
                        _fire_and_forget(self.metrics.record_request(
                            model=model_id, key_label=key.label,
                            status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                            path=path,
                        ), 'metrics')
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
                    await self._record_model_response(model_id, key, resp.status, err_data, path)
                    classification = classify_upstream_error(resp.status, err_data)
                    retryable = self._classify_retry(resp.status, classification)
                    if retryable and attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    return JSONResponse(status_code=resp.status, content=err_data)

                if self.metrics:
                    # F2 fix: DB write off the hot path
                    _fire_and_forget(self.metrics.record_request(
                        model=model_id, key_label=key.label,
                        status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                        path=path,
                    ), 'metrics')

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

        return JSONResponse(status_code=429, headers={'Retry-After': '30'}, content={'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}})

    async def _stream_proxy(self, resp, key):
        # Idle-aware heartbeat: fires even when upstream is silent.
        last_hb = time.time()
        at_line_boundary = True
        saw_done = False
        hb_interval = float(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000')) / 1000.0
        try:
            aiter = resp.content.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(aiter.__anext__(), timeout=hb_interval)
                except asyncio.TimeoutError:
                    if at_line_boundary:
                        yield b': heartbeat\n\n'
                        last_hb = time.time()
                    continue
                except StopAsyncIteration:
                    break
                if isinstance(chunk, (bytes, bytearray)):
                    if b'data: [DONE]' in chunk or b'data:[DONE]' in chunk:
                        saw_done = True
                else:
                    if 'data: [DONE]' in str(chunk) or 'data:[DONE]' in str(chunk):
                        saw_done = True
                yield chunk
                if isinstance(chunk, (bytes, bytearray)) and len(chunk):
                    at_line_boundary = chunk.endswith(b'\n')
                elif chunk:
                    at_line_boundary = str(chunk).endswith('\n')
                last_hb = time.time()
            if not saw_done:
                yield b'data: [DONE]\n\n'
        finally:
            try:
                resp.release()
            except Exception:
                pass
            self.pool.release_success(key)
            self._in_flight = max(0, self._in_flight - 1)

    async def _handle_catch_all(self, request: Request, path: str):
        path = self._normalize_path(path)
        method = request.method
        is_post = method in ('POST', 'PUT', 'PATCH')

        # D11: return 404 for paths that don't match any known API endpoint
        # CRITICAL: Skip catalog/mcp paths entirely - let dedicated handlers handle them
        if path.startswith('/catalog/') or path.startswith('/mcp/'):
            return JSONResponse(status_code=404, content={'error': {'message': f'Unknown endpoint: {path}', 'type': 'invalid_request_error'}})
            
        known_stems = ('/v1/chat/completions', '/v1/completions', '/v1/embeddings',
                       '/v1/models', '/v1/engines', '/v1/images', '/v1/audio',
                       '/v1/moderations', '/v1/responses', '/v1/files',
                       '/v1/fine_tuning', '/v1/batches', '/v1/ranking', '/v1/infer',
                       '/v1/messages', '/v1/messages/count_tokens',
                       '/v1/capabilities', '/v1/capabilities/params',
                       '/v2/', '/api/', '/v1/complete',
                       '/catalog', '/catalog/', '/mcp', '/mcp/')
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
                return JSONResponse(status_code=429, headers={'Retry-After': '30'}, content={'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}})

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

                # P2/V-09: streaming uses read-idle timeout instead of total
                req_timeout = self._client_timeout(is_streaming, path)

                resp = await self._session.request(
                    method, target_url,
                    json=body if is_post else None,
                    headers=fwd_headers,
                    timeout=req_timeout,
                )

                if resp.status == 429:
                    ra = _parse_retry_after(resp.headers.get('retry-after'))  # V-12 fix
                    self._in_flight = max(0, self._in_flight - 1)
                    key.decrement_in_flight()
                    body_text = await resp.text()
                    await self._record_model_response(model_id, key, resp.status, body_text, path)
                    await self.pool.register_rate_limit(key, model_id, ra, None, body_text)
                    if self.metrics:
                        # F2 fix: DB write off the hot path
                        _fire_and_forget(self.metrics.record_rate_limit_event(key_label=key.label, model=model_id, retry_after_s=ra), 'metrics')
                    attempt += 1
                    continue

                content_type = resp.headers.get('content-type', '')
                if ('text/event-stream' in content_type or is_streaming) and resp.status < 400:
                    if self.metrics:
                        # F2 fix: DB write off the hot path
                        _fire_and_forget(self.metrics.record_request(
                            model=model_id, key_label=key.label,
                            status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                            path=path,
                        ), 'metrics')
                    return StreamingResponse(
                        self._stream_proxy(resp, key),
                        media_type='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'},
                    )

                resp_data = await resp.read()
                self._in_flight = max(0, self._in_flight - 1)
                key.decrement_in_flight()

                if resp.status >= 400:
                    # V-07 fix: classify the failure like the other proxy paths —
                    # deterministic 400/404/422 must not be retried across keys.
                    try:
                        err_data = json.loads(resp_data)
                    except (json.JSONDecodeError, ValueError):
                        err_data = {'error': {'message': resp_data.decode('utf-8', errors='replace'), 'type': 'api_error'}}
                    classification = classify_upstream_error(resp.status, err_data)
                    retryable = self._classify_retry(resp.status, classification)
                    if retryable and attempt < max_attempts - 1:
                        attempt += 1
                        continue
                    return JSONResponse(status_code=resp.status, content=err_data)

                if self.metrics:
                    # F2 fix: DB write off the hot path
                    _fire_and_forget(self.metrics.record_request(
                        model=model_id, key_label=key.label,
                        status=resp.status, latency_ms=int((time.time() * 1000) - start_ms),
                        path=path,
                    ), 'metrics')

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

        return JSONResponse(status_code=429, headers={'Retry-After': '30'}, content={'error': {'message': f'All API keys exhausted or rate-limited for model {model_id}', 'type': 'rate_limit_error'}})


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

    if _HAS_SIZE_LIMITER:
        app.add_middleware(RequestSizeLimiter)

    # ── Catalog + MCP Integration (MUST BE BEFORE server routes with catch-all) ──────────────────────
    try:
        from common.catalog_integration import setup_catalog_routes, setup_mcp_server, free_only_enabled as _cfe
        setup_catalog_routes(app)
        setup_mcp_server(app, "nvidia-python")
        # Override free_only with shared version
        free_only_enabled = _cfe
        _HAS_CATALOG_INTEGRATION = True
    except ImportError as _cie:
        _HAS_CATALOG_INTEGRATION = False
        pass

    server = Server(app)
    server._register_routes()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        validate_config()
        logger.info(f'[lifecycle] wrapper-nvidia starting (v{VERSION}, commit={GIT_COMMIT})')
        await server.init()
        try:
            yield
        finally:
            logger.info('[lifecycle] wrapper-nvidia shutting down gracefully...')
            if server:
                # V-17 fix: cancel retained background tasks at shutdown.
                for _task in getattr(server, '_bg_tasks', []):
                    _task.cancel()
                if server._bg_tasks:
                    await asyncio.gather(*server._bg_tasks, return_exceptions=True)
                if server._session:
                    await server._session.close()
                if server._agent:
                    await server._agent.close()
                if server.metrics:
                    await server.metrics.close()
                if server.registry:
                    server.registry.stop()
                await MODEL_REGISTRY_CLIENT.stop()

    app.router.lifespan_context = lifespan
    return app

if __name__ == "__main__":
    main()


# Export app for uvicorn
app = create_app()
