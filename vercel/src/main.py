#!/usr/bin/env python3
import sys
"""
wrapper-vercel — FastAPI proxy for Vercel (similar architecture to wrapper-nvidia).
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
import hmac
from typing import Optional, Set
from pathlib import Path
from contextlib import asynccontextmanager

# Shared persistent catalog/state layer; bootstrap repo root for systemd launches.
try:
    from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../vercel only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter, sanitize_header_value
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

    def sanitize_header_value(value):
        # Fallback sanitizer: upstream common.middleware is missing from the
        # repo, so provide the BUG-SEC2 header-injection guard inline.
        if not isinstance(value, str):
            value = str(value)
        import re as _re
        return _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()
from dotenv import load_dotenv

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from .key_pool import KeyPool

# Circuit breaker for upstream protection
try:
    from common.circuit_breaker import CircuitBreaker, CircuitBreakerError
    _UPSTREAM_BREAKER = CircuitBreaker(failure_threshold=10, recovery_timeout=30, name="vercel-upstream")
    _HAS_CIRCUIT_BREAKER = True
except ImportError:
    _HAS_CIRCUIT_BREAKER = False
from .metrics import Metrics

# ── Shared translations from common/translations (P0 deduplication) ──
# Canonical implementations live in common/translations/. Local definitions
# have been removed — shared versions are imported directly.
try:
    from common.translations import (
        AnthropicStreamState,
        parse_dsml_from_text as _parse_dsml_from_text,
        normalize_upstream_error as _normalize_upstream_error,
        strip_cache_control as _strip_cache,
        repair_orphan_tool_messages as _repair_orphan_tool_messages,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError as _imp_err:
    # OC-7: fail fast at boot instead of starting fine and throwing NameError on
    # the first upstream error / anthropic request (undefined shared helpers).
    raise RuntimeError(
        "common.translations import failed; wrapper requires shared translations"
    ) from _imp_err

if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
# Fallback: also try cwd-relative .env (for direct uvicorn launches)
if not os.environ.get('VERCEL_BASE_URL'):
    if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
        load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/vercel/vercel.log')
try:
    from common.logging_utils import setup_logging
    logger = setup_logging('wrapper-vercel', log_file=LOG_FILE, default_log_file='/tmp/wrapper-vercel.log',
                           log_format='%(asctime)s [vercel] %(message)s')
except ImportError:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        _log_file_handler = logging.FileHandler(LOG_FILE)
    except Exception:
        LOG_FILE = '/tmp/wrapper-vercel.log'
        _log_file_handler = logging.FileHandler(LOG_FILE)
    logger = logging.getLogger('wrapper-vercel')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [vercel] %(message)s',
        handlers=[_log_file_handler, logging.StreamHandler()],
    )

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9105'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
VERCEL_BASE = os.environ.get('VERCEL_BASE_URL', 'https://ai-gateway.vercel.sh/v1').rstrip('/')
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).resolve().parents[1] / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '21600'))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get('MODEL_CATALOG_REFRESH_SEC', '86400'))
MODEL_STORE = ModelStateStore('vercel', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('vercel', profile_db_path=MODEL_STATE_DB)
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
_MODEL_REFRESH_TASK = None
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get('MAX_CONNECTIONS_PER_HOST', '100'))
CONNECT_TIMEOUT_SEC = int(os.environ.get('CONNECT_TIMEOUT_SEC', '30'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT_SEC', '600'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '900'))
# No DEFAULT_MODEL - all model selection is transparent (client chooses)
VERSION = '1.0.5-anthropic-tools'

# Build identity (H-04/H-02): resolve git root + source root from __file__, portable

def validate_config():
    """Validate required configuration at startup."""
    import os
    import sys
    
    missing = []
    for var in ['VERCEL_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    # Validate port range
    try:
        port = int(os.environ.get('LISTEN_PORT', '9105'))
        if not (1024 <= port <= 65535):
            print(f"❌ ERROR: Invalid port {port}")
            sys.exit(1)
    except ValueError:
        print(f"❌ ERROR: LISTEN_PORT must be an integer")
        sys.exit(1)


def _resolve_git_root():
    try:
        import subprocess
        return subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=os.path.dirname(os.path.abspath(__file__)), stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        p = os.path.dirname(os.path.abspath(__file__))
        while p and p != os.path.dirname(p):
            if os.path.isdir(os.path.join(p, '.git')):
                return p
            p = os.path.dirname(p)
        return '/root/wrapper'

def _resolve_git_commit():
    try:
        import subprocess
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=_resolve_git_root(), stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return 'unknown'

GIT_COMMIT = _resolve_git_commit()
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../vercel (up from src/)

def free_only_enabled() -> bool:
    """FREE_ONLY=yes|true|1 → only models with 'free' in the name."""
    v = (os.environ.get('FREE_ONLY') or 'no').strip().lower()
    return v in ('yes', 'true', '1', 'on', 'y')

def is_free_model(model_id: str) -> bool:
    """True if model id contains 'free', or is listed in FREE_MODEL_ALLOWLIST.

    Vercel AI Gateway free catalog mostly uses *-free ids; `big-pickle` is free but
    has no 'free' substring — add it via FREE_MODEL_ALLOWLIST=big-pickle if needed.
    """
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    if mid.startswith('vercel/'):
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
    if raw.lower().startswith('vercel/'):
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
    if mid.lower().startswith('vercel/'):
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

def _bearer_token() -> str:
    """OC-18: re-read BEARER_TOKEN from the environment on every call so that
    edits to .env (hot-reloaded by the watchdog) take effect without a restart,
    instead of being frozen in the module global captured at import time."""
    return (os.environ.get('BEARER_TOKEN') or '').strip()

def _client_ip(request: Request) -> str:
    """OC-8 / DR-7: key rate limiting by the real peer, not a client-supplied
    X-Forwarded-For value (which is trivially spoofable and lets an attacker
    rotate values to bypass the limiter and grow the store unbounded). XFF is
    only trusted as a fallback when no direct peer host is available."""
    host = getattr(request.client, 'host', None) if request.client else None
    if host:
        return host
    xff = request.headers.get('x-forwarded-for')
    if xff:
        return xff.split(',')[0].strip()
    return 'unknown'

# ── Per-IP Rate Limiting ──
from collections import defaultdict
_rate_limit_store = defaultdict(list)
_rate_limit_lock = threading.Lock()
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "120"))

def check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now = time.time()
    with _rate_limit_lock:
        # Sweep: if the store has grown large (e.g. XFF rotation abuse), prune
        # stale timestamps and drop fully-expired keys to bound memory.
        if len(_rate_limit_store) > 1024:
            for k in list(_rate_limit_store.keys()):
                _rate_limit_store[k] = [t for t in _rate_limit_store[k] if now - t < 60]
                if not _rate_limit_store[k]:
                    _rate_limit_store.pop(k, None)
        _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if now - t < 60]
        if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_RPM:
            return False
        _rate_limit_store[client_ip].append(now)
    return True


INFLIGHT_SOFT_CAP = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))

pool = KeyPool()
metrics = Metrics()

_session = None
_session_lock: Optional[asyncio.Lock] = None

def _get_session_lock() -> asyncio.Lock:
    """Lazy-init the session lock on the running event loop."""
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock

async def get_session():
    """Reuse one aiohttp session (fix per-request ClientSession leak).

    Protected by an asyncio.Lock so concurrent calls during session recovery
    never create multiple sessions (BUG-H1 fix).
    """
    global _session
    import aiohttp
    lock = _get_session_lock()
    async with lock:
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

def _model_family(model: str) -> str:
    """Map model id → AI Gateway endpoint family per https://vercel.com/docs/ai-gateway"""
    m = (model or '').lower().strip()
    if m.startswith('vercel/'):
        m = m[len('vercel/'):]
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
    # qwen3.5-plus uses /messages (Anthropic-format); qwen3-coder and other
    # qwen3 variants use /chat/completions (OpenAI-compatible).
    # Per https://vercel.com/docs/ai-gateway (verified 2026-07-26).
    if m.startswith('qwen3.'):
        return 'messages'
    if m.startswith('qwen3') and not m.startswith('qwen3-coder'):
        return 'chat'
    if m.startswith('qwen3-coder'):
        return 'chat'
    # Free models (OpenAI-compatible per AI Gateway docs) → chat/completions
    if is_free_model(m):
        return 'chat'
    # AI Gateway OpenAI-compatible models (Grok, DeepSeek, MiniMax, GLM, Kimi, etc.)
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
    if m.lower().startswith('vercel/'):
        m = m.split('/', 1)[1]
    if is_alias_name(m):
        tgt = get_dynamic_alias_target()
        return tgt if tgt else m
    # Concrete requests never mutate process-wide alias state.
    return m



async def _breaker_outcome(failed: bool):
    """OC-6 / DR-2: actually drive the circuit breaker. Previously only
    before_request() was ever called, so the breaker could never open."""
    if not _HAS_CIRCUIT_BREAKER:
        return
    try:
        if failed:
            await _UPSTREAM_BREAKER.record_failure()
        else:
            await _UPSTREAM_BREAKER.record_success()
    except Exception:
        pass


async def proxy_request(method: str, url: str, json_body: dict = None, headers: dict = None, is_stream: bool = False):
    # Circuit breaker: reject if upstream is failing
    if _HAS_CIRCUIT_BREAKER:
        try:
            await _UPSTREAM_BREAKER.before_request()
        except CircuitBreakerError as cb_err:
            return 503, {"error": {"message": str(cb_err), "type": "service_unavailable"}}

    import aiohttp
    sess = await get_session()
    headers = headers or {}
    try:
        if is_stream:
            # Caller owns release — do NOT async-with the response
            # BUG-OC-STREAM fix: use read-idle timeout instead of hard total
            # timeout. Long generations (reasoning models, agent workflows)
            # can exceed STREAM_REQUEST_TIMEOUT_SEC (15 min). The sock_read
            # timeout detects dead upstream connections without killing
            # legitimate long streams. Parity with nous N-06 and nvidia V-09.
            resp = await sess.request(
                method, url, json=json_body, headers=headers,
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=CONNECT_TIMEOUT_SEC,
                    sock_read=int(os.environ.get('STREAM_SOCK_READ_TIMEOUT_SEC', '300')),
                ),
            )
            if resp.status >= 400:
                text = await resp.text()
                resp.release()
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    data = text
                # NB-2: only 5xx (or transport exceptions) count as breaker
                # failures — client 4xx must not open the breaker (mirror blackbox).
                await _breaker_outcome(resp.status >= 500)
                return resp.status, _normalize_upstream_error(resp.status, data)
            await _breaker_outcome(False)  # OC-6 / DR-2
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
                # NB-2: failure only for status >= 500 (mirror blackbox).
                await _breaker_outcome(resp.status >= 500)
                return resp.status, _normalize_upstream_error(resp.status, data)
            if not isinstance(data, dict):
                data = {"error": {"message": str(data)[:2000], "type": "api_error"}}
            await _breaker_outcome(False)  # OC-6 / DR-2
            return resp.status, data
    except Exception as e:
        await _breaker_outcome(True)  # OC-6 / DR-2
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


# NB-8: strong references for fire-and-forget tasks — asyncio only keeps a weak
# reference to tasks, so an unreferenced create_task() result can be GC'd
# mid-flight. Store here; discard on completion.
_BG_TASKS: Set[asyncio.Task] = set()


def _spawn_bg_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


async def _record_model_result(model_id, key, status, data, url):
    """Persist model-state + schedule observation, off the request hot path."""
    try:
        if status == 200:
            stored = await MODEL_STORE.record_status_async(model_id, credential_fingerprint(key.api_key), 'available', status, 'OK', endpoint=url)
        else:
            stored = await MODEL_STORE.record_error_async(model_id, key.api_key, status, data, endpoint=url)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            'vercel', model_id, stored.get('account_scope', credential_fingerprint(key.api_key)),
            stored.get('state', 'unknown'), status, stored.get('reason_code', ''),
            stored.get('reason_detail', ''), url,
        )
    except Exception as e:
        logger.warning(f'[model-state] Vercel result record failed: {e}')


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
        model_id = json_body.get('model', '') if isinstance(json_body, dict) else ''
        surface = 'anthropic_messages' if '/messages' in url else ('openai_responses' if '/responses' in url else 'openai_chat')
        # OC-2 / DR-13: validate the call-plan + model identity BEFORE spending an
        # upstream request (and a key's quota). Returning here means no live
        # aiohttp response is held, so there is nothing to release on the error
        # branches — eliminating the streaming-connection leak described in OC-2.
        if model_id:
            try:
                call_plan = MODEL_REGISTRY.call_plan(model_id, surface)
                if not same_provider_model_id('vercel', call_plan.model.provider_model_id, model_id):
                    pool.release(key)
                    return 500, {'error': {'type': 'server_error', 'message': 'Model identity changed during call-plan resolution', 'code': 'MODEL_ID_MUTATION'}}, None
            except ValueError as exc:
                pool.release(key)
                return 400, {'error': {'type': 'invalid_request_error', 'message': str(exc), 'code': 'MODEL_CALL_PLAN_INVALID'}}, None
        status, data = await proxy_request(method, url, json_body, headers, is_stream=is_stream)
        if model_id:
            # F3: record model-state/observation off the hot path (fire-and-forget
            # task) instead of awaiting SQLite commits before returning the
            # response — keeps TTFB off the DB write.
            _spawn_bg_task(_record_model_result(model_id, key, status, data, url))  # NB-8
        if status == 200:
            if is_stream:
                return status, data, key
            pool.release(key)
            return status, data, None
        tried += 1
        last_status, last_data = status, data
        classification = classify_upstream_error(status, data)
        if _is_retriable_upstream_status(status, data) and classification['retry_same_model']:
            # NB-6 (OC-13): count OTHER ready keys — the key being marked failed
            # must be excluded, otherwise avail >= 1 always and the
            # available_keys<=0 short-cooldown branch in mark_failure is dead.
            avail = len([k for k in pool.keys if k is not key and not k.is_hard_blocked()])
            if _should_cooldown_key(status, data):
                pool.mark_failure(key, status, _retry_after_seconds(data), 'upstream', available_keys=avail)
            pool.release(key)
            continue
        pool.release(key)
        return status, data, None
    if tried >= max(1, pool.total_keys) and isinstance(last_data, dict) and last_data.get("error"):
        last_data = {"error": {**last_data["error"], "message": f"All configured Vercel keys failed or are rate-limited. Last error: {last_data['error'].get('message', '')}"[:2000]}}
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
    if status >= 400:
        # OC-14: count error responses (previously the `errors` counter could
        # never increment because no caller passed status_code).
        try:
            metrics.record_error(status_code=status)
        except Exception:
            pass
    return JSONResponse(status_code=status, content=content)

def _auth_headers(api_key: str, request: Request = None) -> dict:
    # F5: do not force `Accept-Encoding: identity` — let aiohttp negotiate gzip
    # with upstream (it transparently decompresses for both .text() and the
    # streaming iter_any() reader), saving bandwidth on large generations.
    h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if request is not None:
        for k in ("anthropic-beta", "anthropic-version", "openai-beta", "x-api-key", "x-request-id"):
            v = request.headers.get(k)
            if v:
                # BUG-SEC2 fix: use shared sanitization function
                v = sanitize_header_value(v)
                if v:
                    h[k] = v
    return h

# --- minimal Anthropic <-> OpenAI helpers (surgical, local) ---

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
            elif t == 'image':
                # DR-5: translate Anthropic image blocks to OpenAI image_url data
                # URIs (previously silently dropped → vision requests lost images).
                src = b.get('source') or {}
                if src.get('type') == 'base64':
                    url = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                else:
                    url = src.get('url', '')
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
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
        if len(parts) > 1:
            final = parts
        elif parts:
            p0 = parts[0]
            # NB-7 (DR-5): a single non-text part (e.g. one image block) must be
            # wrapped in a list — a bare part dict as `content` 400s upstream.
            final = p0['text'] if p0.get('type') == 'text' else [p0]
        else:
            final = '' if tools else None
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
    # DR-4: repair orphan tool messages (tool/tool_result without a preceding
    # assistant tool_call) so upstream doesn't 400 on the /messages path.
    out = {"model": model, "messages": _repair_orphan_tool_messages(msgs), "stream": bool(body.get('stream')),
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
_RESPONSE_STORE: dict = {}  # namespaced key -> {"ts": float, "data": list}
_RESPONSE_STORE_TTL_SEC = int(os.environ.get('RESPONSE_STORE_TTL_SEC', '3600'))
_RESPONSE_STORE_MAX_CHARS = int(os.environ.get('RESPONSE_STORE_MAX_CHARS', '4000000'))
_RESPONSE_STORE_MAX_ENTRY_CHARS = int(os.environ.get('RESPONSE_STORE_MAX_ENTRY_CHARS', '500000'))


def _store_response(principal: str, key: str, data) -> None:
    """OC-11: namespace conversation history by auth principal so a guessed
    `previous_response_id` cannot read another tenant's turns; bound per-entry
    size and expire stale entries on a TTL.

    NB-10: trim without repeatedly re-serializing the whole payload on the
    event loop (the old drop-one/re-dumps loop was O(n^2) over multi-MB JSON
    → multi-second stalls). Each entry is serialized exactly once; huge
    single-turn entries are truncated before storage; trimming drops oldest
    turns using the precomputed per-entry sizes.
    """
    global _RESPONSE_STORE
    if isinstance(data, list):
        entries, sizes = [], []
        for item in data:
            try:
                s_len = len(json.dumps(item, ensure_ascii=False))
            except Exception:
                s_len = len(str(item))
            if s_len > _RESPONSE_STORE_MAX_ENTRY_CHARS:
                role = item.get('role', 'user') if isinstance(item, dict) else 'user'
                item = {"role": role, "content": f"[truncated by wrapper: entry of {s_len} chars exceeded RESPONSE_STORE_MAX_ENTRY_CHARS]"}
                s_len = len(json.dumps(item, ensure_ascii=False))
            entries.append(item)
            sizes.append(s_len + 2)  # +2 ≈ JSON list separator overhead
        total = sum(sizes)
        start = 0
        while start < len(entries) and total > _RESPONSE_STORE_MAX_CHARS:
            total -= sizes[start]
            start += 1
        data = entries[start:]
    store_key = f"{principal}\x00{key}"
    _RESPONSE_STORE[store_key] = {"ts": time.time(), "data": data}
    # Evict expired entries and bound total size to 200.
    if len(_RESPONSE_STORE) > 200:
        now = time.time()
        for k in [k for k, v in _RESPONSE_STORE.items() if now - v["ts"] > _RESPONSE_STORE_TTL_SEC]:
            _RESPONSE_STORE.pop(k, None)
        if len(_RESPONSE_STORE) > 200:
            for k in sorted(_RESPONSE_STORE, key=lambda k: _RESPONSE_STORE[k]["ts"])[:len(_RESPONSE_STORE) - 200]:
                _RESPONSE_STORE.pop(k, None)


def _load_response(principal: str, key: str):
    entry = _RESPONSE_STORE.get(f"{principal}\x00{key}")
    if not entry:
        return None
    if time.time() - entry["ts"] > _RESPONSE_STORE_TTL_SEC:
        _RESPONSE_STORE.pop(f"{principal}\x00{key}", None)
        return None
    return entry["data"]



def responses_to_chat(body: dict, principal: str = '') -> dict:
    model = _normalize_model(body.get('model') or '')
    msgs = []
    # G11 + OC-11: if previous_response_id references a stored conversation,
    # prepend it — looked up under the caller's principal namespace.
    prev = body.get('previous_response_id')
    if prev:
        _prev = _load_response(principal, prev)
        if _prev:
            msgs.extend(_prev)
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

async def _chunk_stream(resp):
    """Yield upstream chunks with idle-heartbeat signalling.

    Yields `(True, None)` on an idle tick (no chunk within HEARTBEAT_MS) and
    `(False, chunk)` for each received chunk. Uses an asyncio.wait sentinel so
    heartbeats fire during upstream idle gaps (reasoning models silent for
    30-120s) — not only after a chunk arrives (BUG-CODEX2). Cancels the pending
    read task on exit so a client disconnect never leaks a dangling awaiter.
    """
    chunk_iter = resp.content.iter_any().__aiter__()
    chunk_task = None
    try:
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            done_set, _pending = await asyncio.wait({chunk_task}, timeout=HEARTBEAT_MS / 1000.0)
            if not done_set:
                yield (True, None)
                continue
            finished, chunk_task = chunk_task, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                break
            # NB-4: TimeoutError/ClientError are NOT swallowed here — an upstream
            # mid-stream failure must propagate so callers (stream_passthrough /
            # responses gen) surface an error instead of synthesizing a clean
            # [DONE]/response.completed with truncated text (mirror blackbox
            # _iter_chunks_with_idle, which propagates errors).
            yield (False, chunk)
    finally:
        if chunk_task is not None:
            chunk_task.cancel()


async def stream_passthrough(resp, key, heartbeat=True, terminal_done=True):
    """Yield upstream SSE bytes + proxy heartbeats; always release key/resp.

    For OpenAI-compatible streams, synthesize a final data: [DONE] on upstream
    EOF without one. Anthropic native pass-through disables this because its
    terminal event is message_stop, not [DONE].

    OC-4 / DR-1: heartbeats fire during upstream idle gaps via _chunk_stream.
    OC-5: the heartbeat comment is only injected after a clean SSE line boundary
    (the last yielded byte was a newline) so it never splits a `data:` line.
    """
    last_hb = time.time()
    saw_done = False
    buffer_ends_newline = True
    try:
        async for idle, chunk in _chunk_stream(resp):
            if idle:
                if heartbeat and (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                    if buffer_ends_newline:  # OC-5: line-aligned injection
                        yield b": heartbeat\n\n"
                        last_hb = time.time()
                continue
            if isinstance(chunk, (bytes, bytearray)):
                if b"data: [DONE]" in chunk or b"data:[DONE]" in chunk:
                    saw_done = True
            else:
                if "data: [DONE]" in str(chunk) or "data:[DONE]" in str(chunk):
                    saw_done = True
            yield chunk
            if chunk:
                buffer_ends_newline = chunk[-1:] in (b"\n", b"\r")
        if terminal_done and not saw_done:
            yield b"data: [DONE]\n\n"
    finally:
        try:
            resp.release()
        except Exception:
            pass
        pool.release(key)

_env_observer = None  # OC-18: kept so it can be stopped on shutdown


def start_env_watcher():
    global _env_observer
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
        _env_observer = obs
        logger.info('[env] Watching .env')
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')


async def _metrics_persist_loop():
    # OC-14: persist metrics periodically so counters survive SIGKILL/OOM
    # (previously only written on graceful shutdown).
    interval = int(os.environ.get('METRICS_PERSIST_SEC', '60'))
    while True:
        await asyncio.sleep(interval)
        try:
            metrics._persist()
        except Exception:
            pass

class _CatalogRequest:
    headers = {}


async def refresh_model_catalog_once():
    """Refresh the persistent Vercel catalog independently of user traffic."""
    try:
        status, data, _ = await proxy_request_with_pool(
            "GET", f"{VERCEL_BASE}/models", None, _CatalogRequest()
        )
        models_data = data.get("data") or data.get("models") or [] if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source="vercel:/models")
            MODEL_REGISTRY.register_catalog(models_data, revision="runtime-catalog")
            MODEL_REGISTRY_CLIENT.schedule_catalog("vercel", models_data, "runtime-catalog")
            logger.info(f"[model-catalog] Vercel refreshed {len(models_data)} models")
    except Exception as e:
        logger.warning(f"[model-catalog] Vercel refresh failed: {e}")


async def model_catalog_refresh_loop():
    while True:
        await asyncio.sleep(max(60, MODEL_CATALOG_REFRESH_SEC))
        await refresh_model_catalog_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _session, _MODEL_REFRESH_TASK, _METRICS_PERSIST_TASK, _env_observer
    pool.load_from_env()
    start_env_watcher()
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or '').strip()
    if seed and not is_alias_name(seed):
        set_dynamic_alias_target(seed, force=True)
        MODEL_REGISTRY.bind_explicit_aliases(seed, _ALIAS_NAME_SET, scope_type="wrapper", scope_id="vercel")
    logger.info(f"wrapper-vercel starting on {BIND_HOST}:{LISTEN_PORT} base={VERCEL_BASE} alias_target={get_dynamic_alias_target() or 'none'}")
    await MODEL_REGISTRY_CLIENT.start()
    # OC-17: refresh the model catalog at boot instead of serving stale/fallback
    # data for up to MODEL_CATALOG_REFRESH_SEC (default 1 day) before the first
    # background refresh.
    # NB-14: bound the boot refresh — previously awaited unbounded (up to the
    # 600s request timeout) before the app started serving. On timeout the
    # periodic loop / next /v1/models call picks it up.
    try:
        _boot_refresh_timeout = float(os.environ.get('BOOT_CATALOG_REFRESH_TIMEOUT_SEC', '15'))
        await asyncio.wait_for(refresh_model_catalog_once(), timeout=_boot_refresh_timeout)
    except asyncio.TimeoutError:
        logger.warning('[model-catalog] boot refresh timed out; serving with cached/fallback catalog')
    except Exception as _boot_exc:
        logger.warning(f'[model-catalog] boot refresh failed: {_boot_exc}')
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    _METRICS_PERSIST_TASK = asyncio.create_task(_metrics_persist_loop())  # OC-14
    yield

    # Graceful shutdown: wait for in-flight requests
    logger.info(f"[vercel] Starting graceful shutdown...")
    shutdown_start = time.time()
    max_wait = 30
    while shutdown_start + max_wait > time.time():
        total = sum(k.in_flight for k in pool.keys)
        if total == 0:
            logger.info(f"[vercel] All requests drained")
            break
        await asyncio.sleep(0.1)
    logger.info('[lifecycle] wrapper-vercel shutting down gracefully...')
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _MODEL_REFRESH_TASK = None
    if _METRICS_PERSIST_TASK:  # OC-14: final flush before shutdown
        _METRICS_PERSIST_TASK.cancel()
        try:
            await _METRICS_PERSIST_TASK
        except asyncio.CancelledError:
            pass
        try:
            metrics._persist()
        except Exception:
            pass
        _METRICS_PERSIST_TASK = None
    if _env_observer is not None:  # OC-18: stop the watchdog observer on shutdown
        try:
            _env_observer.stop()
            _env_observer.join(timeout=2)
        except Exception:
            pass
        _env_observer = None
    await MODEL_REGISTRY_CLIENT.stop()
    await metrics.close()  # Persist metrics snapshot to disk
    if _session is not None and not _session.closed:
        await _session.close()
    logger.info("Shutdown complete")

app = FastAPI(title="wrapper-vercel", version=VERSION, lifespan=lifespan)


# Request latency tracking middleware
@app.middleware("http")
async def add_latency_tracking(request: Request, call_next):
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    latency_ms = (time.time() - start_time) * 1000
    request_id = request.headers.get("x-request-id", "N/A")
    
    logger.info(
        f"[{app.title}] request_id={request_id} "
        f"method={request.method} path={request.url.path} "
        f"latency={latency_ms:.2f}ms status={response.status_code}"
    )
    
    response.headers["X-Process-Time"] = f"{latency_ms:.2f}ms"
    return response


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

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter)

def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return  # CORS preflight passes without auth
    if os.environ.get('DISABLE_AUTH'):
        return  # pre-auth mode: allow all (LAN/open)
    # G10 fix: if BEARER_TOKEN is set, auth is mandatory and must match.
    # If client sends a token (even wrong) we MUST reject on mismatch.
    # If BEARER_TOKEN empty, remain open (backwards-compatible, logged).
    token = _bearer_token()  # OC-18: re-read so .env rotation takes effect
    if not token:
        if request.headers.get("authorization") or request.headers.get("x-api-key"):
            logger.warning("[auth] BEARER_TOKEN unset but client sent credentials — accepting open (insecure)")
        return
    auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    client_token = auth.replace("Bearer ", "", 1).strip()
    # SEC-5: constant-time comparison to avoid timing side-channels on the token.
    # NB-11: compare as bytes — compare_digest raises TypeError (→ 500) on
    # non-ASCII str input; encoding both sides makes it a clean 401 instead.
    if not client_token or not hmac.compare_digest(
            client_token.encode('utf-8'), token.encode('utf-8')):
        raise HTTPException(401, {"error": {"type": "authentication_error", "message": "Unauthorized"}})

@app.get("/health")
async def health():
    return {"status": "ok" if pool.available_keys > 0 else "degraded", "version": VERSION, "git_commit": GIT_COMMIT, "source_root": SOURCE_ROOT, "pid": os.getpid(), "keys": pool.total_keys, "available": pool.available_keys, "live_keys": pool.all_stats(), "free_only": free_only_enabled(), "dynamic_alias_target": get_dynamic_alias_target() or None, "base": VERCEL_BASE, "metrics": await metrics.summary(), "model_registry": MODEL_REGISTRY_CLIENT.stats(), "models_cached": len(await asyncio.to_thread(MODEL_STORE.get_ids, False))}


@app.get("/ready")
async def ready(request: Request):
    _auth_check(request)
    try:
        status, data, _ = await proxy_request_with_pool("GET", f"{VERCEL_BASE}/models", None, request)
        return {"ready": status == 200, "upstream_ok": status == 200, "status_code": status, "last_error": None if status == 200 else (data.get("error") if isinstance(data, dict) else str(data)), "keys": pool.total_keys, "available": pool.available_keys}
    except Exception as e:
        return _jr(503, {"ready": False, "upstream_ok": False, "last_error": str(e), "keys": pool.total_keys, "available": pool.available_keys})

@app.get("/v1/models")
async def models(request: Request):
    """Proxy AI Gateway GET /models — public endpoint for model discovery (no auth required)."""
    # /v1/models is intentionally public: agents need model discovery before auth
    tgt = get_dynamic_alias_target()
    fallback_all = [
        {"id": "gpt-5.4-mini", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "claude-haiku-4-5", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "claude-opus-4-6", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "anthropic/claude-3.5-sonnet", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "openai/gpt-4o-mini", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "google/gemini-2.0-flash", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "meta/llama-3.3-70b-instruct", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "deepseek/deepseek-chat", "object": "model", "owned_by": "vercel-ai-gateway"},
        {"id": "mistralai/mistral-small-latest", "object": "model", "owned_by": "vercel-ai-gateway"},
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

    # OC-9: these are synchronous SQLite reads — run them off the event loop so
    # concurrent traffic (and slow disk / WAL contention) never stalls in-flight
    # streams.
    cached = await asyncio.to_thread(MODEL_STORE.get_catalog, True)
    try:
        if cached:
            data = {'data': cached}
        else:
            status, data, _ = await proxy_request_with_pool("GET", f"{VERCEL_BASE}/models", None, request)
            if status == 200 and isinstance(data, dict) and (data.get('data') or data.get('models')):
                _models = data.get('data') or data.get('models') or []
                await asyncio.to_thread(MODEL_STORE.upsert_catalog, _models, 'vercel:/models')
                await asyncio.to_thread(MODEL_REGISTRY.register_catalog, _models, 'runtime-catalog')
                MODEL_REGISTRY_CLIENT.schedule_catalog('vercel', _models, 'runtime-catalog')
            elif status != 200 or not isinstance(data, dict):
                stale = await asyncio.to_thread(MODEL_STORE.get_catalog, False)
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
        status_map = await asyncio.to_thread(MODEL_STORE.status_map)
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
        models_list = await asyncio.to_thread(MODEL_STORE.get_catalog, False)
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
    import uuid
    import time
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start_time = time.time()
    """OpenAI Chat — routes to AI Gateway /chat/completions (or native family if model demands it)."""
    _auth_check(request)
    _ip = _client_ip(request)
    if not check_rate_limit(_ip):  # OC-8 / DR-7: rate-limit all POST endpoints
        return _jr(429, {"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}})
    if not isinstance(body, dict):  # F6: malformed (non-object) JSON body → 400, not 500
        return _jr(400, {"error": {"type": "invalid_request_error", "message": "Request body must be a JSON object"}})
    if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
        return _jr(400, {"error": {"type": "invalid_request_error", "message": "max_tokens must be a positive integer"}})
    # BUG-SEC3 fix: cap max_tokens to prevent overflow
    if isinstance(body.get("max_tokens"), int) and body["max_tokens"] > 1000000:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": "max_tokens exceeds maximum allowed value of 1000000"}})
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
    family = _model_family(body.get("model") or "")
    # OC-12: gemini/google family was incorrectly routed to the catalog URL
    # `{base}/models/{model}` (a GET listing), which 404/405s and cooled down a
    # key. Route it through the OpenAI-compatible chat endpoint like the rest.
    url = f"{VERCEL_BASE}/chat/completions"

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
    """OpenAI Responses — AI Gateway native path is /responses for GPT* models.
    For chat-family models, translate Responses→Chat→Responses.
    """
    _auth_check(request)
    _ip = _client_ip(request)
    if not check_rate_limit(_ip):  # OC-8 / DR-7
        return _jr(429, {"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {"error": {"type": "invalid_request_error", "message": f"Invalid JSON: {e}"}})
    if not isinstance(body, dict):  # F6: malformed (non-object) JSON body → 400, not 500
        return _jr(400, {"error": {"type": "invalid_request_error", "message": "Request body must be a JSON object"}})
    # DR-9 / BUG-SEC3: cap max_tokens on the Responses surface too (chat already
    # enforces this; the responses translate path did not).
    for _mt_key in ('max_output_tokens', 'max_tokens'):
        _mt = body.get(_mt_key)
        if isinstance(_mt, int) and _mt > 1000000:
            return _jr(400, {"error": {"type": "invalid_request_error", "message": f"{_mt_key} exceeds maximum allowed value of 1000000"}})
    principal = _bearer_token() or 'anon'  # OC-11 namespace
    requested = body.get("model")  # transparent: never inject DEFAULT_MODEL
    model = _normalize_model(requested) if requested else ""
    if requested is not None:
        body["model"] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return _jr(400, free_only_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return _jr(400, free_only_error(requested or model))
    is_stream = bool(body.get("stream", False))
    family = _model_family(model)

    try:
        if family == "responses":
            # Native AI Gateway Responses passthrough
            url = f"{VERCEL_BASE}/responses"
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
                return _jr(502, {"error": {"message": f"AI Gateway upstream error: {e}", "type": "api_error"}})
            if status != 200:
                err_data = data if isinstance(data, dict) else {"error": {"message": str(data)}}
                return _jr(status, err_data)
            # AI Gateway may return {"type":"error",...} even with 200 in some paths — normalize
            if isinstance(data, dict) and data.get("type") == "error":
                return _jr(400, {"error": data.get("error", {"message": "AI Gateway error", "type": "api_error"})})
            return JSONResponse(data)

        # Translate via chat/completions for non-GPT AI Gateway models
        chat_body = responses_to_chat(body, principal)
        chat_body["stream"] = is_stream
        url = f"{VERCEL_BASE}/chat/completions"
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
                    except (json.JSONDecodeError, ValueError):
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
                    last_hb = time.time()
                    async for idle, chunk in _chunk_stream(resp):
                        if idle:
                            # OC-4 / DR-1: heartbeat during upstream idle gaps.
                            if (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                                yield ": heartbeat\n\n"
                                last_hb = time.time()
                            continue
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
                    # OC-10 / O22: do NOT fabricate an error string as assistant
                    # output (Codex would persist it as a successful answer). Emit
                    # a proper failure event and stop.
                    yield emit("response.failed", {"response": {"id": rid, "model": model, "status": "failed", "error": {"type": "api_error", "message": f"Upstream stream error: {e}"}}})
                    return
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
                _store_response(principal, rid, list(chat_body.get("messages", [])) + [_assistant_message_from_chat({}, acc_text, completed_tools)])
            return StreamingResponse(gen(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

        status, data, _ = await proxy_request_with_pool("POST", url, chat_body, request)
        if status != 200:
            return _jr(status, data if isinstance(data, dict) else {"error": {"message": str(data)}})
        resp_obj = chat_to_responses(model, data)
        # G11: store conversation for previous_response_id multi-turn
        rid_store = resp_obj.get("id")
        if rid_store:
            _store_response(principal, rid_store, list(chat_body.get("messages", [])) + [_assistant_message_from_chat(data)])
        # G11 also store under the request's response id if provided
        if body.get("previous_response_id") is None and body.get("id"):
            _store_response(principal, body["id"], chat_body.get("messages", []))
        return JSONResponse(resp_obj)
    except Exception as e:
        return _jr(502, {"error": {"message": str(e), "type": "api_error"}})




@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    _auth_check(request)
    if not check_rate_limit(_client_ip(request)):  # OC-8 / DR-7: key by real peer, not spoofable XFF
        return _jr(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    if not isinstance(body, dict):  # F6: malformed (non-object) JSON body → 400, not 500
        return _jr(400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'Request body must be a JSON object'}})
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
    family = _model_family(model)

    try:
        if family == "messages":
            url = f"{VERCEL_BASE}/messages"
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
        url = f"{VERCEL_BASE}/chat/completions"
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
                    last_hb = time.time()
                    async for idle, chunk in _chunk_stream(resp):
                        if idle:
                            # OC-4 / DR-1: heartbeat during upstream idle gaps.
                            if (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                                yield ": heartbeat\n\n"
                                last_hb = time.time()
                            continue
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
                            except (json.JSONDecodeError, ValueError):
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
        "provider": "vercel",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": await asyncio.to_thread(MODEL_STORE.status_map),
    }


@app.get("/dashboard")
@app.get("/dashboard.html")
async def dashboard(request: Request):
    """Serve the wrapper dashboard HTML.

    OC-3: require auth like every other endpoint, and NEVER embed the bearer
    token into the served HTML — that leaked the token to any local page/process
    able to reach the dashboard.
    """
    _auth_check(request)
    from pathlib import Path
    dashboard_path = Path(__file__).parent.parent / "dashboard.html"
    if not dashboard_path.exists():
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content="<html><body><h1>Dashboard not found</h1></body></html>")
    html = dashboard_path.read_text()
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


@app.get("/version")
async def version():
    return {"version": VERSION, "git_commit": GIT_COMMIT, "source_root": SOURCE_ROOT, "pid": os.getpid()}

@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    return _jr(404, {"error": {"message": f"Unsupported: /{path}", "type": "not_found_error"}})

def main():
    import uvicorn
    uvicorn.run("src.main:app", host=BIND_HOST, port=LISTEN_PORT, log_level="info")



# ── Catalog + MCP Integration ──────────────────────────────────────────
try:
    from common.catalog_integration import setup_catalog_routes, setup_mcp_server, free_only_enabled as _cfe
    setup_catalog_routes(app)
    setup_mcp_server(app, "vercel")
    # Override free_only with shared version
    free_only_enabled = _cfe
    _HAS_CATALOG_INTEGRATION = True
except ImportError as _cie:
    _HAS_CATALOG_INTEGRATION = False
    pass

if __name__ == "__main__":
    main()
