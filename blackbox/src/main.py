#!/usr/bin/env python3
"""wrapper-blackbox — BLACKBOX AI OpenAI + Anthropic compatible proxy.

BLACKBOX's public API is OpenAI-compatible at https://api.blackbox.ai.  This
wrapper exposes the same monorepo contract as nvidia-python, nous, and opencode:
Chat Completions, Responses API, Anthropic Messages, multi-key retry/cooldown,
structured tools, dynamic aliases, and strict stream finalization.
"""

from __future__ import annotations
import sys

import os
import hmac
import json
import time
import threading
import asyncio
import logging
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

import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../blackbox only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter, sanitize_header_value
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

# B-28/B-29/B-30 fix: shared, fail-closed auth (see common/auth.py).
try:
    from common.auth import check_auth as _shared_check_auth
    _HAS_SHARED_AUTH = True
except ImportError:  # pragma: no cover - common/ always present in-repo
    _HAS_SHARED_AUTH = False

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

from .metrics import Metrics

# ── Shared translations from common/translations (P0 deduplication) ──
# BB-8/DR-8: fail HARD at import time if the shared translation layer is
# missing. Previously a broken PYTHONPATH/partial deploy booted "fine" and
# then raised NameError on the first request that touched
# _normalize_upstream_error / AnthropicStreamState / _strip_cache.
from common.translations import (
    AnthropicStreamState,
    normalize_upstream_error as _normalize_upstream_error,
    strip_cache_control as _strip_cache,
    repair_orphan_tool_messages as _repair_orphan_tool_messages,
    parse_dsml_from_text as _parse_dsml_from_text,  # BB-14/DR-6
    parse_retry_after as _parse_retry_after,
    is_retriable_status as _is_retriable_status,
    should_cooldown_key as _should_cooldown_key,
    build_forward_headers as _build_forward_headers,
    sanitize_header_value,
    anthropic_to_openai_response,
    openai_to_anthropic_response,
    stream_anthropic_to_openai,
)

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
    load_dotenv(ROOT / '.env')
    load_dotenv()

LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/blackbox/blackbox.log')
try:
    from common.logging_utils import setup_logging
    logger = setup_logging('wrapper-blackbox', log_file=LOG_FILE, default_log_file='/tmp/wrapper-blackbox.log',
                           log_format='%(asctime)s [blackbox] %(message)s')
except ImportError:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        _log_file_handler = logging.FileHandler(LOG_FILE)
    except Exception:
        LOG_FILE = '/tmp/wrapper-blackbox.log'
        _log_file_handler = logging.FileHandler(LOG_FILE)
    logger = logging.getLogger('wrapper-blackbox')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [blackbox] %(message)s',
                        handlers=[_log_file_handler, logging.StreamHandler()])

LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9104'))
# BB-10/F4: default to loopback. Binding to 0.0.0.0 must be an explicit,
# deliberate LISTEN_HOST override — a manual `python -m src.main` run must
# never silently expose the proxy to the network.
BIND_HOST = os.environ.get('LISTEN_HOST', '127.0.0.1')
BLACKBOX_BASE = os.environ.get('BLACKBOX_BASE_URL', 'https://api.blackbox.ai').rstrip('/')
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(Path(__file__).resolve().parents[1] / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '21600'))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get('MODEL_CATALOG_REFRESH_SEC', '86400'))
MODEL_STORE = ModelStateStore('blackbox', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('blackbox', profile_db_path=MODEL_STATE_DB)
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
_MODEL_REFRESH_TASK = None
BEARER_TOKEN = os.environ.get('BEARER_TOKEN', '').strip()



def validate_config():
    """Validate required configuration at startup."""
    import os
    import sys
    
    missing = []
    for var in ['BLACKBOX_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    # Validate port range
    try:
        port = int(os.environ.get('LISTEN_PORT', '9104'))
        if not (1024 <= port <= 65535):
            print(f"❌ ERROR: Invalid port {port}")
            sys.exit(1)
    except ValueError:
        print(f"❌ ERROR: LISTEN_PORT must be an integer")
        sys.exit(1)


def _bearer_token() -> str:
    """Re-read BEARER_TOKEN from the environment on every call so .env
    hot-reloads (watchdog) take effect without a restart (opencode parity)."""
    return (os.environ.get('BEARER_TOKEN') or '').strip()


def _env_flag(name: str, default: str = 'yes') -> bool:
    return (os.environ.get(name) or default).strip().lower() in ('yes', 'true', '1', 'on', 'y')


# ── Transparency gates (documented in .env.example) ──
# B11: fill `content: null → ""` / zero-usage defaults in chat responses
# (SDK-compat mutation; disable for verbatim upstream bodies).
def _compat_fill_defaults() -> bool:
    return _env_flag('COMPAT_FILL_DEFAULTS', 'yes')


# B12: drop nameless tools from /v1/chat/completions requests (upstream-4xx
# avoidance mutation; disable to forward `tools` verbatim).
def _clean_tools_enabled() -> bool:
    return _env_flag('CLEAN_TOOLS', 'yes')


# B7 (opt-in): pass upstream error bodies through verbatim instead of
# normalizing them to OpenAI error shape.
def _raw_upstream_errors() -> bool:
    return _env_flag('RAW_UPSTREAM_ERRORS', 'no')


def _client_ip(request: Request) -> str:
    """BB-11/DR-7: key rate limiting by the real peer, not the client-supplied
    X-Forwarded-For header (spoofable → limiter bypass + unbounded store
    growth). XFF is only a fallback when no direct peer host is available."""
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
_rate_limit_last_sweep = 0.0
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "600"))

def check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    if RATE_LIMIT_RPM <= 0:
        return True  # per-IP limiting disabled (multi-agent setups)
    global _rate_limit_last_sweep
    now = time.time()
    with _rate_limit_lock:
        # BB-11: periodic global sweep so stale keys cannot grow unbounded.
        if now - _rate_limit_last_sweep > 300:
            _rate_limit_last_sweep = now
            for k in list(_rate_limit_store.keys()):
                pruned = [t for t in _rate_limit_store[k] if now - t < 60]
                if pruned:
                    _rate_limit_store[k] = pruned
                else:
                    del _rate_limit_store[k]
        _rate_limit_store[client_ip] = [t for t in _rate_limit_store[client_ip] if now - t < 60]
        if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_RPM:
            return False
        _rate_limit_store[client_ip].append(now)
    return True


HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
MAX_CONNECTIONS = int(os.environ.get('MAX_CONNECTIONS', '200'))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get('MAX_CONNECTIONS_PER_HOST', '100'))
CONNECT_TIMEOUT_SEC = int(os.environ.get('CONNECT_TIMEOUT_SEC', '30'))
REQUEST_TIMEOUT_SEC = int(os.environ.get('REQUEST_TIMEOUT_SEC', '600'))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get('STREAM_REQUEST_TIMEOUT_SEC', '900'))
VERSION = '1.0.0-contract'

# Build identity (H-04/H-02): resolve git root + source root from __file__, portable
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
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../blackbox (up from src/)

# Curated discovery manifest only; it never substitutes an inference model.
CURATED_FREE_MODELS = [
    {'id': 'blackboxai/nvidia/nemotron-3-super-120b-a12b:free', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
    {'id': 'blackboxai/x-ai/grok-code-fast-1:free', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
    {'id': 'blackboxai/nvidia/nemotron-nano-12b-v2-vl', 'object': 'model', 'owned_by': 'blackbox', 'supports_tools': True},
]

_ALIAS_NAME_SET = {
    'sonnet', 'opus', 'haiku',
    'claude-sonnet', 'claude-opus', 'claude-haiku',
    'claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4',
    'claude-sonnet-4-20250514', 'claude-opus-4-20250514', 'claude-haiku-4-20250514',
    'claude-sonnet-4-5', 'claude-opus-4-5', 'claude-haiku-4-5',
    'claude-sonnet-4-6', 'claude-opus-4-6', 'claude-3-5-sonnet-20241022', 'claude-3-5-haiku-20241022',
}
_dynamic_alias_target = ''
_dynamic_alias_lock = threading.Lock()
_known_models = {m['id'] for m in CURATED_FREE_MODELS}

pool = KeyPool()
metrics = Metrics()
_session = None
_session_lock = None


def _get_session_lock():
    """Lazy-init the session lock on the running event loop (BUG-H1 fix)."""
    global _session_lock
    if _session_lock is None:
        _session_lock = asyncio.Lock()
    return _session_lock


def free_only_enabled() -> bool:
    return (os.environ.get('FREE_ONLY') or 'no').strip().lower() in ('yes', 'true', '1', 'on', 'y')


def _allowlist() -> set[str]:
    raw = os.environ.get('FREE_MODEL_ALLOWLIST') or 'blackboxai/nvidia/nemotron-nano-12b-v2-vl'
    return {x.strip().lower() for x in raw.split(',') if x.strip()}


def is_free_model(model_id: str) -> bool:
    if not model_id:
        return False
    mid = str(model_id).strip().lower()
    bare = mid.split('/')[-1]
    if 'free' in mid or mid in _allowlist() or bare in _allowlist():
        return True
    return False


def free_only_error(model_id: str) -> dict:
    return {'error': {'type': 'invalid_request_error', 'message': f'Model "{model_id}" is blocked by FREE_ONLY=yes. Send a free model id, add it to FREE_MODEL_ALLOWLIST, or set FREE_ONLY=no.', 'code': 'free_only_restricted', 'param': 'model'}}


def free_only_anthropic_error(model_id: str) -> dict:
    return {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': free_only_error(model_id)['error']['message']}}


def is_alias_name(model_id: str) -> bool:
    return bool(model_id) and str(model_id).lower().strip() in _ALIAS_NAME_SET


def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ''


def set_dynamic_alias_target(model_id: str, force: bool = False) -> None:
    global _dynamic_alias_target
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if not mid:
        return
    if free_only_enabled() and not is_free_model(mid) and not force:
        return
    if not force and _known_models and mid not in _known_models and free_only_enabled() and not is_free_model(mid):
        logger.debug(f'[alias] ignoring unknown non-free model {mid!r}')
        return
    with _dynamic_alias_lock:
        if _dynamic_alias_target != mid:
            logger.info(f'[alias] dynamic target bound -> {mid}')
        _dynamic_alias_target = mid


def _normalize_model(model: str) -> str:
    if model is None:
        return ''
    m = str(model).strip()
    if not m:
        return ''
    if is_alias_name(m):
        return get_dynamic_alias_target() or m
    # Concrete requests never mutate process-wide alias state.
    return m


def model_allowed(model_id: str) -> bool:
    if not free_only_enabled():
        return True
    if is_alias_name(model_id):
        tgt = get_dynamic_alias_target()
        return bool(tgt and is_free_model(tgt))
    return is_free_model(model_id)


async def get_session():
    """Reuse one aiohttp session with lock protection (BUG-H1 fix)."""
    global _session
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


# TRANSPARENT PROXY: forward ALL client headers via shared build_forward_headers.
# The old 5-item allowlist was too narrow — it dropped x-stainless-* (SDK
# identity), x-correlation-id (tracing), accept-language, and other standard
# headers that clients send. The shared allowlist covers all standard
# OpenAI/Anthropic SDK headers + common agent identity headers.
_FORWARD_HEADER_DENYLIST = {'authorization', 'x-api-key', 'host', 'content-length', 'content-type',
                            'connection', 'transfer-encoding', 'accept-encoding', 'cookie'}


def _forward_extra_headers() -> tuple:
    """B3 (transparency, opt-in): FORWARD_EXTRA_HEADERS is a comma-separated
    list of additional client header names to forward upstream (sanitized)."""
    raw = os.environ.get('FORWARD_EXTRA_HEADERS') or ''
    return tuple(h.strip().lower() for h in raw.split(',')
                 if h.strip() and h.strip().lower() not in _FORWARD_HEADER_DENYLIST)


def _auth_headers(api_key: str, request: Request = None) -> dict:
    """Build upstream headers: Authorization swap + transparent client header forwarding.

    Per project principle #1 (TRANSPARENT PROXY): forward ALL client headers
    via shared build_forward_headers (broad allowlist) so client identity,
    beta-feature flags, tracing IDs, and SDK metadata reach upstream unchanged.
    """
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    accept_encoding = (os.environ.get('UPSTREAM_ACCEPT_ENCODING') or '').strip()
    if accept_encoding:
        headers['Accept-Encoding'] = accept_encoding
    if request is not None:
        forwarded = _build_forward_headers(request.headers)
        for k, v in forwarded.items():
            headers[k] = v
        # Also forward any operator-configured extra headers.
        for k in _forward_extra_headers():
            v = request.headers.get(k)
            if v:
                v = sanitize_header_value(v)
                if v:
                    headers[k] = v
    return headers




async def proxy_request(method: str, url: str, json_body: dict = None, headers: dict = None, is_stream: bool = False):
    """Transparent proxy: forward request to upstream, return (status, data).

    On 429, parses the HTTP Retry-After header and embeds it in the error
    dict so proxy_request_with_pool can cool down the key for the correct
    duration (anti rate-limit).
    """
    import aiohttp as _aiohttp
    sess = await get_session()
    headers = headers or {}

    def _upstream_error_body(status: int, data):
        # B7 (opt-in transparency): RAW_UPSTREAM_ERRORS=yes passes upstream
        # error bodies through verbatim instead of normalizing shape.
        if _raw_upstream_errors() and isinstance(data, dict):
            return data
        return _normalize_upstream_error(status, data)

    try:
        if is_stream:
            resp = await sess.request(method, url, json=json_body, headers=headers, timeout=_aiohttp.ClientTimeout(
                total=None,
                sock_connect=CONNECT_TIMEOUT_SEC,
                sock_read=int(os.environ.get('STREAM_SOCK_READ_TIMEOUT_SEC', '300')),
            ))
            if resp.status >= 400:
                text = await resp.text()
                # Parse Retry-After header for 429 cooldown (anti rate-limit).
                retry_after = _parse_retry_after(resp.headers, None) if resp.status == 429 else 0
                resp.release()
                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    data = text
                err = _upstream_error_body(resp.status, data)
                if retry_after and isinstance(err, dict):
                    err.setdefault('error', {})['retry_after'] = retry_after
                return resp.status, err
            return 200, resp
        async with sess.request(method, url, json=json_body, headers=headers, timeout=_aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC)) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = text
            if resp.status >= 400:
                # Parse Retry-After header for 429 cooldown (anti rate-limit).
                retry_after = _parse_retry_after(resp.headers, data if isinstance(data, dict) else None) if resp.status == 429 else 0
                err = _upstream_error_body(resp.status, data)
                if retry_after and isinstance(err, dict):
                    err.setdefault('error', {})['retry_after'] = retry_after
                return resp.status, err
            if not isinstance(data, dict):
                data = {'error': {'message': str(data)[:2000], 'type': 'api_error'}}
            return resp.status, data
    except Exception as e:
        # SEC-6: truncate transport exception text in client-facing 502s.
        return 502, {'error': {'message': str(e)[:2000], 'type': 'api_error'}}


def _retry_after_seconds(data, default=65) -> int:
    """Delegate to shared parse_retry_after. Kept for backward compat."""
    if isinstance(data, tuple) and len(data) == 2:
        headers, body = data
        return _parse_retry_after(headers, body, default)
    return _parse_retry_after(None, data if isinstance(data, dict) else None, default)


def _is_retriable_upstream_status(status: int) -> bool:
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


def _looks_model_capacity_error(data) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    return any(x in blob for x in ('no deployments available', 'selected model', 'cooldown_list', 'invalid model name', 'model unavailable'))


# B-21 fix: the local `_should_cooldown_key` that used to live here SHADOWED
# the `should_cooldown_key` imported from common.translations, so cooldown
# policy silently diverged per wrapper — exactly the drift
# CROSS_WRAPPER_BUG_POLICY.md exists to prevent. The model-capacity carve-out
# has been promoted into the shared implementation; this module now uses it
# directly (imported above as _should_cooldown_key).


_BACKGROUND_TASKS: set = set()


def _spawn_background(coro):
    """Fire-and-forget with a strong reference so the task is not GC'd."""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _record_model_result(model_id: str, api_key: str, status: int, data, url: str):
    """F3 (latency): model-state persistence runs as a background task so the
    SQLite commit is never awaited on the TTFB path."""
    try:
        if status == 200:
            stored = await MODEL_STORE.record_status_async(model_id, credential_fingerprint(api_key), 'available', status, 'OK', endpoint=url)
        else:
            stored = await MODEL_STORE.record_error_async(model_id, api_key, status, data, endpoint=url)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            'blackbox', model_id, stored.get('account_scope', credential_fingerprint(api_key)),
            stored.get('state', 'unknown'), status, stored.get('reason_code', ''),
            stored.get('reason_detail', ''), url,
        )
    except Exception as e:
        logger.warning(f'[model-state] Blackbox result record failed: {e}')


async def proxy_request_with_pool(method: str, url: str, json_body: dict, request: Request, is_stream: bool = False):
    """NO MODEL FALLBACK: retry model A across keys, never substitute model B.

    The model id is extracted once and reused for EVERY retry. Model-scoped
    blocks ensure error on model A at key1 only blocks key1 for model A;
    model B can still use key1.
    """
    attempts = max(1, pool.total_keys)
    last_status = 429
    last_data = {'error': {'message': 'No capacity — all keys exhausted or rate-limited', 'type': 'rate_limit_error'}}
    tried = 0
    model_id = json_body.get('model', '') if isinstance(json_body, dict) else ''
    # BB-2/DR-13/B9: validate the call plan BEFORE any upstream request.
    # Previously this ran after the call, which (a) leaked the live streaming
    # response on rejection (connection-pool exhaustion) and (b) burned
    # upstream quota, then voided an already-successful response.
    if model_id:
        surface = 'anthropic_messages' if '/messages' in url else ('openai_responses' if '/responses' in url else 'openai_chat')
        try:
            call_plan = MODEL_REGISTRY.call_plan(model_id, surface)
            if not same_provider_model_id('blackbox', call_plan.model.provider_model_id, model_id):
                return 500, {'error': {'type': 'server_error', 'message': 'Model identity changed during call-plan resolution', 'code': 'MODEL_ID_MUTATION'}}, None
        except ValueError as exc:
            return 400, {'error': {'type': 'invalid_request_error', 'message': str(exc), 'code': 'MODEL_CALL_PLAN_INVALID'}}, None
    for _ in range(attempts):
        key_result = await pool.acquire(model_id)
        if not key_result:
            break
        key = key_result['key']
        headers = _auth_headers(key.api_key, request)
        status, data = await proxy_request(method, url, json_body, headers, is_stream=is_stream)
        if model_id:
            # F3: fire-and-forget; never await SQLite on the response path.
            _spawn_background(_record_model_result(model_id, key.api_key, status, None if status == 200 or not isinstance(data, (dict, str)) else data, url))
        if status == 200:
            if is_stream:
                return status, data, key
            pool.release(key)
            return status, data, None
        tried += 1
        last_status, last_data = status, data
        classification = classify_upstream_error(status, data)
        if _is_retriable_upstream_status(status) and classification['retry_same_model']:
            if _should_cooldown_key(status, data):
                pool.mark_failure(key, status, _retry_after_seconds(data), 'upstream', model=model_id)
            pool.release(key)
            continue
        pool.release(key)
        return status, data, None
    if tried >= max(1, pool.total_keys) and isinstance(last_data, dict) and last_data.get('error'):
        last_data = {'error': {**last_data['error'], 'message': f"All configured Blackbox keys failed or are rate-limited. Last error: {last_data['error'].get('message', '')}"[:2000]}}
    return last_status, last_data, None


def _ensure_chat_message(data: dict) -> dict:
    # B11 (transparency): SDK-compat fill (`content: null → ""`, zero-usage
    # default) is gated behind COMPAT_FILL_DEFAULTS (default yes, documented).
    # Set COMPAT_FILL_DEFAULTS=no to forward the upstream body verbatim.
    if not _compat_fill_defaults():
        return data
    if not isinstance(data, dict):
        return data
    try:
        choices = data.get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            if msg.get('content') is None:
                msg['content'] = ''
            choices[0]['message'] = msg
            data['choices'] = choices
        data.setdefault('usage', {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})
    except Exception:
        pass
    return data




def anthropic_to_openai(body: dict) -> dict:
    _strip_cache(body)
    model = _normalize_model(body.get('model') or '')
    msgs = []
    sys = body.get('system')
    if isinstance(sys, str) and sys:
        msgs.append({'role': 'system', 'content': sys})
    elif isinstance(sys, list):
        texts = [s.get('text', str(s)) if isinstance(s, dict) else str(s) for s in sys]
        if texts:
            msgs.append({'role': 'system', 'content': '\n'.join(texts)})
    for m in body.get('messages') or []:
        role, c = m.get('role'), m.get('content')
        if isinstance(c, str):
            msgs.append({'role': role, 'content': c})
            continue
        if not isinstance(c, list):
            msgs.append({'role': role, 'content': c if c is not None else ''})
            continue
        parts, tools, reasoning = [], [], []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get('type')
            if t == 'text':
                parts.append({'type': 'text', 'text': b.get('text', '')})
            elif t == 'image':
                src = b.get('source') or {}
                if src.get('type') == 'base64':
                    url = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                else:
                    url = src.get('url', '')
                if url:
                    parts.append({'type': 'image_url', 'image_url': {'url': url}})
            elif t == 'thinking':
                reasoning.append(b.get('thinking') or '')
            elif t == 'tool_use':
                tools.append({'id': b.get('id'), 'type': 'function', 'function': {'name': b.get('name') or '', 'arguments': json.dumps(b.get('input') or {}, ensure_ascii=False)}})
            elif t == 'tool_result':
                rc = b.get('content')
                txt = rc if isinstance(rc, str) else '\n'.join(x.get('text', '') for x in (rc or []) if isinstance(x, dict))
                msgs.append({'role': 'tool', 'tool_call_id': b.get('tool_use_id') or b.get('id') or '', 'content': txt})
        final = parts if len(parts) > 1 else (parts[0]['text'] if parts else ('' if tools else None))
        if role == 'user' and not parts and not tools and not reasoning:
            continue
        if role == 'assistant' and not parts and not tools and not reasoning:
            continue
        am = {'role': role, 'content': final if final is not None else ('' if tools else None)}
        if tools:
            am['tool_calls'] = tools
            if am.get('content') is None:
                am['content'] = ''
        if reasoning:
            am['reasoning_content'] = '\n'.join(reasoning)
        msgs.append(am)
    out = {'model': model, 'messages': _repair_orphan_tool_messages(msgs), 'stream': bool(body.get('stream'))}
    # TRANSPARENT PROXY: only set max_tokens if the client explicitly sent one.
    # Never inject a default (was 4096) — that mutates client intent.
    mt = body.get('max_tokens')
    if mt is not None:
        out['max_tokens'] = mt
    # Forward ALL client params verbatim (transparent proxy — no silent drops).
    # Ported from opencode's 15-param list for cross-wrapper normalization.
    param_map = [
        ('temperature', 'temperature'), ('top_p', 'top_p'), ('top_k', 'top_k'),
        ('stop_sequences', 'stop'), ('seed', 'seed'),
        ('parallel_tool_calls', 'parallel_tool_calls'),
        ('frequency_penalty', 'frequency_penalty'),
        ('presence_penalty', 'presence_penalty'),
        ('logit_bias', 'logit_bias'), ('logprobs', 'logprobs'),
        ('top_logprobs', 'top_logprobs'), ('response_format', 'response_format'),
        ('service_tier', 'service_tier'), ('user', 'user'), ('metadata', 'metadata'),
    ]
    for src, dst in param_map:
        if body.get(src) is not None:
            out[dst] = body[src]
    tc = body.get('tool_choice')
    if tc is not None:
        if isinstance(tc, dict):
            t = tc.get('type')
            if t == 'auto':
                out['tool_choice'] = 'auto'
            elif t == 'any':
                out['tool_choice'] = 'required'
            elif t == 'tool' and tc.get('name'):
                out['tool_choice'] = {'type': 'function', 'function': {'name': tc['name']}}
            else:
                out['tool_choice'] = tc
        else:
            out['tool_choice'] = tc
    if body.get('tools'):
        tools = []
        for t in body.get('tools') or []:
            if not isinstance(t, dict) or not t.get('name'):
                continue
            tools.append({'type': 'function', 'function': {'name': t['name'], 'description': t.get('description', '') or '', 'parameters': t.get('input_schema') or {}}})
        if tools:
            out['tools'] = tools
    return out


def _parse_tool_args(s: str):
    try:
        parsed = json.loads(s or '{}')
        return parsed if isinstance(parsed, dict) else {'value': parsed}
    except Exception:
        return {'raw': s or ''}


def openai_to_anthropic(model: str, data: dict) -> dict:
    if isinstance(data, dict) and data.get('type') == 'message' and 'content' in data:
        return data
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    content = []
    if reasoning:
        content.append({'type': 'thinking', 'thinking': reasoning})
    tool_calls = list(msg.get('tool_calls') or [])
    # BB-14/DR-6: parse MiniMax DSML tool markup leaked into visible content
    # into structured tool_use blocks instead of passing it to clients raw
    # (opencode parity via shared common.translations helper).
    dsml_tools = []
    if isinstance(text, str) and 'DSML' in text.replace('｜', '|'):
        text, dsml_tools = _parse_dsml_from_text(text)
    if text or (not tool_calls and not dsml_tools):
        content.append({'type': 'text', 'text': text if isinstance(text, str) else str(text)})
    content.extend(dsml_tools)
    for tc in tool_calls:
        fn = tc.get('function') or {}
        content.append({'type': 'tool_use', 'id': tc.get('id') or f"toolu_{int(time.time()*1000)}", 'name': fn.get('name', '') or '', 'input': _parse_tool_args(fn.get('arguments', ''))})
    if not content:
        content.append({'type': 'text', 'text': ''})
    fr = (data.get('choices') or [{}])[0].get('finish_reason')
    stop = 'tool_use' if (tool_calls or dsml_tools) else {'tool_calls': 'tool_use', 'stop': 'end_turn', 'length': 'max_tokens', 'content_filter': 'refusal'}.get(fr, 'end_turn')
    u = data.get('usage') or {}
    return {'id': data.get('id') or f"msg_{int(time.time()*1000)}", 'type': 'message', 'role': 'assistant', 'model': model, 'content': content, 'stop_reason': stop, 'stop_sequence': None, 'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0, 'output_tokens': u.get('completion_tokens', 0) or 0}}


_RESPONSE_STORE: dict[str, tuple] = {}  # key -> (ts, size, messages)

def _extract_principal(request) -> str:
    """Extract a stable tenant identifier from the request for store namespacing.

    BUG-SEC-RESPONSE-STORE fix (2026-07-28): keys are namespaced by auth
    principal to prevent cross-tenant data leaks. Priority: Bearer token >
    x-api-key > client IP > 'anonymous'. Uses a SHA-256 fingerprint (first
    24 chars) to avoid storing raw credentials as dictionary keys.
    """
    import hashlib
    token = ''
    try:
        auth = request.headers.get('authorization', '') or request.headers.get('x-api-key', '')
        if auth:
            token = auth.replace('Bearer ', '', 1).strip() if auth.lower().startswith('bearer ') else auth.strip()
        if not token and request.client:
            token = request.client.host or ''
    except Exception:
        pass
    if not token:
        return 'anonymous'
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]

def _response_store_key(principal: str, rid: str) -> str:
    """Namespace a response ID by the caller's principal for tenant isolation."""
    return f"{principal}\x00{rid}"




def responses_to_chat(body: dict, principal: str = '') -> dict:
    model = _normalize_model(body.get('model') or '')
    msgs = []
    prev = body.get('previous_response_id')
    # BUG-SEC-RESPONSE-STORE fix: lookup must use namespaced key so that
    # a client can only read its own stored conversations, never another
    # tenant's. Without this, any previous_response_id value would match
    # across tenants — a cross-tenant data leak.
    if prev and principal:
        # B-33: TTL-aware read from the bounded store.
        msgs.extend(_get_stored_conversation(principal, prev))
    raw = body.get('input')
    if isinstance(raw, str):
        msgs.append({'role': 'user', 'content': raw})
    elif isinstance(raw, list):
        for it in raw:
            if isinstance(it, str):
                msgs.append({'role': 'user', 'content': it})
                continue
            if not isinstance(it, dict):
                continue
            t = it.get('type')
            if t == 'function_call_output':
                outv = it.get('output', '')
                msgs.append({'role': 'tool', 'tool_call_id': it.get('call_id') or '', 'content': outv if isinstance(outv, str) else json.dumps(outv, ensure_ascii=False)})
            elif t == 'function_call':
                args = it.get('arguments', '')
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                msgs.append({'role': 'assistant', 'content': None, 'tool_calls': [{'id': it.get('call_id') or it.get('id') or 'call_1', 'type': 'function', 'function': {'name': it.get('name', '') or '', 'arguments': args}}]})
            else:
                role = it.get('role', 'user')
                if role == 'developer':
                    role = 'system'
                c = it.get('content', '')
                if isinstance(c, list):
                    c = ''.join(p.get('text', '') for p in c if isinstance(p, dict) and p.get('type') in ('input_text', 'text', 'output_text'))
                msgs.append({'role': role or 'user', 'content': c})
    if body.get('instructions'):
        if msgs and msgs[0].get('role') == 'system':
            msgs[0]['content'] = body['instructions'] + '\n\n' + str(msgs[0].get('content') or '')
        else:
            msgs.insert(0, {'role': 'system', 'content': body['instructions']})
    msgs = _repair_orphan_tool_messages(msgs)
    out = {'model': model, 'messages': msgs, 'stream': bool(body.get('stream', False))}
    # BB-12/DR-9/B16: forward the client value verbatim — no bare int() cast
    # (a non-numeric value now yields a shaped 400 via _validate_chat_body in
    # the endpoint instead of an unhandled ValueError → raw 500), and the
    # client's explicit value is never overridden.
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = body['max_output_tokens']
    elif body.get('max_tokens') is not None:
        out['max_tokens'] = body['max_tokens']
    # B16 (transparency): forward client params verbatim (no float() casts,
    # no silent drops of stop/seed/parallel_tool_calls/…).
    for k in ('temperature', 'top_p', 'tool_choice', 'stop', 'seed',
              'parallel_tool_calls', 'stream_options', 'user', 'metadata',
              'frequency_penalty', 'presence_penalty', 'logit_bias',
              'logprobs', 'top_logprobs', 'response_format', 'service_tier'):
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
                continue
            tools.append({'type': 'function', 'function': {'name': name, 'description': fn.get('description', '') or '', 'parameters': fn.get('parameters') or fn.get('input_schema') or {}}})
        if tools:
            out['tools'] = tools
    return out


def chat_to_responses(model: str, data: dict) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    output = []
    # B18 (transparency): surface upstream reasoning_content as a reasoning
    # output item (opencode parity) instead of silently discarding it.
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    if reasoning:
        output.append({'id': f"rsn_{int(time.time()*1000)}", 'type': 'reasoning', 'status': 'completed', 'text': reasoning})
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function') or {}
        output.append({'id': tc.get('id') or f'fc_{len(output)}', 'type': 'function_call', 'status': 'completed', 'call_id': tc.get('id'), 'name': fn.get('name', '') or '', 'arguments': fn.get('arguments', '') or ''})
    output.append({'id': f"msg_{int(time.time()*1000)}", 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
    u = data.get('usage') or {}
    return {'id': data.get('id') or f"resp_{int(time.time()*1000)}", 'object': 'response', 'created_at': int(time.time()), 'model': model, 'status': 'completed', 'output': output, 'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0, 'output_tokens': u.get('completion_tokens', 0) or 0, 'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}}


def _assistant_message_from_chat(data: dict, fallback_text: str = '', tool_accs=None) -> dict:
    msg = (data.get('choices') or [{}])[0].get('message', {}) if isinstance(data, dict) else {}
    content = msg.get('content')
    if content is None:
        content = fallback_text if fallback_text is not None else None
    tool_calls = msg.get('tool_calls') or []
    if tool_accs:
        tool_calls = [{'id': acc.get('call_id'), 'type': 'function', 'function': {'name': acc.get('name', ''), 'arguments': acc.get('args', '')}} for acc in tool_accs if acc]
    out = {'role': 'assistant', 'content': content if content not in ('', None) else (None if tool_calls else '')}
    if tool_calls:
        out['tool_calls'] = tool_calls
    return out


_IDLE = object()  # sentinel yielded by _iter_chunks_with_idle when upstream is silent


async def _iter_chunks_with_idle(resp, idle_sec: float):
    """BB-5/DR-1 (BUG-CODEX2): yield upstream chunks as they arrive, and yield
    the _IDLE sentinel whenever the upstream has been silent for `idle_sec`,
    so callers can emit heartbeats even while the model is thinking.

    Port of the nous `wrapper_nous.py` pattern (sentinel task + asyncio.wait
    instead of asyncio.wait_for, so a genuine upstream read timeout surfaces
    as an error instead of being mistaken for an idle tick)."""
    chunk_iter = resp.content.iter_any().__aiter__()
    chunk_task = None
    try:
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            done_set, _pending = await asyncio.wait({chunk_task}, timeout=idle_sec)
            if not done_set:
                yield _IDLE
                continue
            finished, chunk_task = chunk_task, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        if chunk_task is not None:
            chunk_task.cancel()


async def stream_passthrough(resp, key, heartbeat=True):
    last_hb = time.time()
    saw_done = False
    # BB-6: iter_any() chunks are NOT SSE-line aligned; a heartbeat comment
    # injected mid-line corrupts the SSE frame. Only heartbeat when the last
    # forwarded byte ended a line.
    at_line_boundary = True
    try:
        async for chunk in _iter_chunks_with_idle(resp, HEARTBEAT_MS / 1000.0):
            now = time.time()
            if chunk is _IDLE:
                # BB-5/DR-1: upstream silent (reasoning model) — heartbeat so
                # client/LB idle timeouts do not kill the stream.
                if heartbeat and at_line_boundary and (now - last_hb) > (HEARTBEAT_MS / 1000.0):
                    yield b': heartbeat\n\n'
                    last_hb = now
                continue
            chunk_text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, (bytes, bytearray)) else str(chunk)
            if 'data: [DONE]' in chunk_text or 'data:[DONE]' in chunk_text:
                saw_done = True
            yield chunk
            if isinstance(chunk, (bytes, bytearray)) and len(chunk):
                at_line_boundary = chunk.endswith(b'\n')
            elif chunk_text:
                at_line_boundary = chunk_text.endswith('\n')
            if heartbeat and at_line_boundary and (time.time() - last_hb) > (HEARTBEAT_MS / 1000.0):
                yield b': heartbeat\n\n'
                last_hb = time.time()
        if not saw_done:
            yield b'data: [DONE]\n\n'
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
                    load_dotenv(ROOT / '.env', override=True)
                    # Reload the key pool so new BLACKBOX_API_KEY_* entries
                    # take effect without a process restart.
                    try:
                        pool.load_from_env()
                    except Exception as e:
                        logger.warning(f'[env] pool reload failed: {e}')
                    logger.info('[env] .env hot-reloaded')
        obs = Observer()
        obs.schedule(EnvWatcher(), path=str(ROOT), recursive=False)
        obs.start()
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')


class _CatalogRequest:
    headers = {}


async def refresh_model_catalog_once():
    """Refresh the persistent Blackbox catalog independently of user traffic."""
    try:
        status, data, _ = await proxy_request_with_pool(
            'GET', f'{BLACKBOX_BASE}/models', None, _CatalogRequest()
        )
        models_data = (data.get('data') or data.get('models') or []) if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source='blackbox:/models')
            MODEL_REGISTRY.register_catalog(models_data, revision='runtime-catalog')
            MODEL_REGISTRY_CLIENT.schedule_catalog('blackbox', models_data, 'runtime-catalog')
            logger.info(f'[model-catalog] Blackbox refreshed {len(models_data)} models')
    except Exception as e:
        logger.warning(f'[model-catalog] Blackbox refresh failed: {e}')


async def model_catalog_refresh_loop():
    while True:
        await asyncio.sleep(max(60, MODEL_CATALOG_REFRESH_SEC))
        await refresh_model_catalog_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # B-22: `global _session` removed — never assigned in this scope.
    global _MODEL_REFRESH_TASK
    pool.load_from_env()
    seed = (os.environ.get('DYNAMIC_ALIAS_TARGET') or '').strip()
    if seed and not is_alias_name(seed):
        set_dynamic_alias_target(seed, force=True)
        MODEL_REGISTRY.bind_explicit_aliases(seed, _ALIAS_NAME_SET, scope_type="wrapper", scope_id="blackbox")
    start_env_watcher()
    logger.info(f'wrapper-blackbox starting on {BIND_HOST}:{LISTEN_PORT} base={BLACKBOX_BASE} free_only={free_only_enabled()} alias_target={get_dynamic_alias_target() or None}')
    # BB-3/DR-10/SEC-2: loud startup warning when running with auth disabled.
    if not _bearer_token():
        logger.warning('[auth] SECURITY WARNING: BEARER_TOKEN is empty — ALL endpoints are UNAUTHENTICATED. '
                       'Any local process can use the paid upstream keys. Set BEARER_TOKEN in blackbox/.env.')
    await MODEL_REGISTRY_CLIENT.start()
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    yield

    # Graceful shutdown: wait for in-flight requests
    logger.info(f"[blackbox] Starting graceful shutdown...")
    shutdown_start = time.time()
    max_wait = 30
    while shutdown_start + max_wait > time.time():
        total = sum(k.in_flight for k in pool.keys)
        if total == 0:
            logger.info(f"[blackbox] All requests drained")
            break
        await asyncio.sleep(0.1)
    logger.info('[lifecycle] wrapper-blackbox shutting down gracefully...')
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _MODEL_REFRESH_TASK = None
    await MODEL_REGISTRY_CLIENT.stop()
    await metrics.close()  # Persist metrics snapshot to disk
    if _session is not None and not _session.closed:
        await _session.close()


app = FastAPI(title='wrapper-blackbox', version=VERSION, lifespan=lifespan)


# Request latency tracking middleware
@app.middleware("http")
async def add_latency_tracking(request: Request, call_next):
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    latency_ms = (time.time() - start_time) * 1000
    request_id = request.headers.get("x-request-id", "N/A")
    
    logger.info(
        f"[blackbox] request_id={request_id} "
        f"method={request.method} path={request.url.path} "
        f"latency={latency_ms:.2f}ms status={response.status_code}"
    )
    
    response.headers["X-Process-Time"] = f"{latency_ms:.2f}ms"
    return response



@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and ('error' in detail or detail.get('type') == 'error'):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={'error': {'type': 'api_error', 'message': str(detail)}})


app.add_middleware(CORSMiddleware, allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$', allow_methods=['*'], allow_headers=['*'], expose_headers=['*'], allow_credentials=True)


# R-01 fix: reject non-object JSON bodies with a shaped 400 instead of letting
# `body.get(...)` raise AttributeError -> HTTP 500 (see common/body_guard.py).
try:
    from common.body_guard import JSONBodyGuard as _JSONBodyGuard
    app.add_middleware(_JSONBodyGuard)
except ImportError:  # pragma: no cover
    pass

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter)


def _auth_check(request: Request):
    """B-28/B-29/B-30 fix: delegate to the shared, fail-closed implementation.

    Previously this returned early (allowing ALL requests) when BEARER_TOKEN
    was unset, and compared `str` operands with hmac.compare_digest — which
    raises TypeError → 500 on non-ASCII tokens instead of a clean 401.
    """
    if request.method == 'OPTIONS':
        return  # CORS preflight passes without auth
    if _HAS_SHARED_AUTH:
        res = _shared_check_auth(request.headers, surface=request.url.path)
        if not res.ok:
            raise HTTPException(res.status, {'error': {
                'type': 'authentication_error', 'message': res.message}})
        return
    # Fallback (common/ unavailable): still fail closed.
    if os.environ.get('DISABLE_AUTH'):
        return
    token = _bearer_token()
    if not token:
        raise HTTPException(503, {'error': {
            'type': 'authentication_error', 'message': 'Server auth not configured'}})
    auth = request.headers.get('authorization', '') or request.headers.get('x-api-key', '')
    client_token = auth.replace('Bearer ', '', 1).strip()
    if not client_token or not hmac.compare_digest(
            client_token.encode('utf-8'), token.encode('utf-8')):
        raise HTTPException(401, {'error': {'type': 'authentication_error', 'message': 'Unauthorized'}})


@app.get('/health')
async def health():
    return {'status': 'ok' if pool.available_keys > 0 else 'degraded', 'version': VERSION, 'git_commit': GIT_COMMIT, 'source_root': SOURCE_ROOT, 'pid': os.getpid(), 'keys': pool.total_keys, 'available': pool.available_keys, 'live_keys': pool.all_stats(), 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None, 'base': BLACKBOX_BASE, 'metrics': await metrics.summary(), 'model_registry': MODEL_REGISTRY_CLIENT.stats(), 'models_cached': len(await asyncio.to_thread(MODEL_STORE.get_ids, False))}


@app.get('/ready')
async def ready(request: Request):
    _auth_check(request)
    try:
        status, data, _ = await proxy_request_with_pool('GET', f'{BLACKBOX_BASE}/models', None, request)
        return {'ready': status == 200, 'upstream_ok': status == 200, 'status_code': status, 'last_error': None if status == 200 else (data.get('error') if isinstance(data, dict) else str(data)), 'keys': pool.total_keys, 'available': pool.available_keys}
    except Exception as e:
        return JSONResponse(status_code=503, content={'ready': False, 'upstream_ok': False, 'last_error': str(e), 'keys': pool.total_keys, 'available': pool.available_keys})


@app.get('/version')
async def version():
    return {'version': VERSION, 'git_commit': GIT_COMMIT, 'source_root': SOURCE_ROOT, 'pid': os.getpid()}


@app.get('/v1/models')
async def models(request: Request):
    # /v1/models is intentionally public: agents need model discovery before auth
    # BB-9/DR-12: fetch the status map ONCE per request (opencode parity —
    # previously status_for() ran a full-table SQLite scan per model), and run
    # the synchronous SQLite calls off the event loop.
    try:
        status_map = await asyncio.to_thread(MODEL_STORE.status_map)
    except Exception:
        status_map = {}
    try:
        cached = await asyncio.to_thread(MODEL_STORE.get_catalog, True)
    except Exception:
        cached = []
    fallback = {'object': 'list', 'data': _model_list_with_aliases(CURATED_FREE_MODELS, status_map), 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None}
    try:
        if cached:
            upstream = cached
        else:
            status, data, _ = await proxy_request_with_pool('GET', f'{BLACKBOX_BASE}/models', None, request)
            upstream = (data.get('data') or data.get('models') or []) if status == 200 and isinstance(data, dict) else []
            if upstream:
                MODEL_STORE.upsert_catalog(upstream, source='blackbox:/models')
                MODEL_REGISTRY.register_catalog(upstream, revision='runtime-catalog')
                MODEL_REGISTRY_CLIENT.schedule_catalog('blackbox', upstream, 'runtime-catalog')
            else:
                upstream = await asyncio.to_thread(MODEL_STORE.get_catalog, False)
        normalized = []
        for m in upstream:
            entry = m if isinstance(m, dict) else {'id': str(m), 'object': 'model', 'owned_by': 'blackbox'}
            if entry.get('id'):
                _known_models.add(entry['id'])
                if not free_only_enabled() or model_allowed(entry['id']):
                    normalized.append(entry)
        if not normalized:
            normalized = fallback['data']
        return {'object': 'list', 'data': _model_list_with_aliases(normalized, status_map), 'free_only': free_only_enabled(), 'dynamic_alias_target': get_dynamic_alias_target() or None, 'catalog_cached': bool(cached)}
    except Exception as e:
        logger.warning(f'models fallback: {e}')
        return fallback


@app.get('/api/tags')
async def api_tags():
    """Ollama-compatible model discovery — PUBLIC (no auth).

    Returns the model list in Ollama's /api/tags format so Ollama clients
    can discover models served by this wrapper.
    """
    try:
        cached = await asyncio.to_thread(MODEL_STORE.get_catalog, True)
    except Exception:
        cached = []
    out_models = []
    for m in (cached or []):
        if not isinstance(m, dict):
            continue
        mid = m.get('id', '')
        if not mid:
            continue
        family = mid.split('/')[0] if '/' in mid else mid
        out_models.append({
            'name': mid, 'model': mid,
            'modified_at': '1970-01-01T00:00:00Z', 'size': 0, 'digest': '',
            'details': {
                'parent_model': '', 'format': 'gguf',
                'family': family, 'families': [family],
                'parameter_size': '', 'quantization_level': '',
            },
        })
    return {'models': out_models}


def _model_list_with_aliases(models_in: list, status_map: dict | None = None) -> list:
    # BB-9/DR-12: index one pre-fetched status_map() instead of running a
    # full-table scan per model on the event loop.
    status_map = status_map or {}
    data = []
    seen = set()
    for m in models_in:
        if not isinstance(m, dict):
            continue
        mid = m.get('id')
        if not mid or mid in seen:
            continue
        if free_only_enabled() and not model_allowed(mid):
            continue
        seen.add(mid)
        state = status_map.get(mid) or {}
        data.append({
            **m,
            'object': m.get('object', 'model'),
            'catalog_listed': True,
            'availability_state': state.get('state', 'unknown'),
            'availability_scope': 'account',
            'reason_code': state.get('reason_code', ''),
            'checked_at': state.get('checked_at'),
        })
    tgt = get_dynamic_alias_target()
    for alias in ('sonnet', 'opus', 'haiku'):
        if alias in seen:
            continue
        if free_only_enabled() and not (tgt and is_free_model(tgt)):
            continue
        data.append({'id': alias, 'object': 'model', 'owned_by': 'alias', 'dynamic_alias': True, 'rooted_model': tgt or None})
    return data


@app.get('/v1/capabilities')
async def capabilities(request: Request):
    # BB-13/DR-11: require auth (opencode parity) — account-scoped
    # availability must not be enumerable without credentials.
    _auth_check(request)
    model_data = (await models(request)).get('data', [])
    return {'object': 'list', 'models': [{'id': m.get('id'), 'capabilities': ['chat', 'completion'], 'streaming': True} for m in model_data if isinstance(m, dict)], 'summary': {'total': len(model_data), 'by_type': {'chat': len(model_data)}}, 'dynamic_alias_target': get_dynamic_alias_target() or None}


@app.post('/v1/messages/count_tokens')
async def count_tokens(request: Request):
    _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    return {'input_tokens': max(1, len(json.dumps(body, ensure_ascii=False)) // 4)}


def _validate_chat_body(body: dict):
    if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
        return {'error': {'type': 'invalid_request_error', 'message': 'max_tokens must be a positive integer'}}
    # BUG-SEC3 fix: cap max_tokens to prevent overflow
    if isinstance(body.get('max_tokens'), int) and body['max_tokens'] > 1000000:
        return {'error': {'type': 'invalid_request_error', 'message': 'max_tokens exceeds maximum allowed value of 1000000'}}
    for m in body.get('messages', []) or []:
        if isinstance(m, dict) and m.get('role') not in (None, 'system', 'user', 'assistant', 'tool', 'developer', 'function'):
            return {'error': {'type': 'invalid_request_error', 'message': f"Invalid role: {m.get('role')!r}"}}
        if isinstance(m, dict) and m.get('role') == 'tool' and not m.get('tool_call_id'):
            return {'error': {'type': 'invalid_request_error', 'message': 'tool role requires tool_call_id'}}
    return None


def _clean_tools(body: dict):
    # B12 (transparency): dropping nameless tools / deleting an empty `tools`
    # key is a request mutation — gated behind CLEAN_TOOLS (default yes,
    # documented). Set CLEAN_TOOLS=no to forward `tools` verbatim.
    if not _clean_tools_enabled():
        return
    if isinstance(body.get('tools'), list):
        cleaned = []
        for tool in body['tools']:
            if not isinstance(tool, dict):
                continue
            fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
            name = fn.get('name') if isinstance(fn, dict) else None
            if name:
                cleaned.append(tool)
        if cleaned:
            body['tools'] = cleaned
        else:
            body.pop('tools', None)


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    # B-23 fix: the previous `request_id = ...` / `start_time = ...` locals here
    # were computed and then never used — dead code implying per-request
    # observability that did not exist. Correlation ID and latency are set
    # centrally by the HTTP middleware (X-Request-ID / X-Process-Time), so the
    # duplicated locals are removed rather than reimplemented here.
    _auth_check(request)
    if not check_rate_limit(_client_ip(request)):
        return JSONResponse(status_code=429, content={"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    err = _validate_chat_body(body)
    if err:
        return JSONResponse(status_code=400, content=err)
    requested = body.get('model')
    if requested is not None:
        body['model'] = _normalize_model(requested)
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(body.get('model') or ''):
        return JSONResponse(status_code=400, content=free_only_error(requested))
    if free_only_enabled() and body.get('model') and not model_allowed(body['model']):
        return JSONResponse(status_code=400, content=free_only_error(requested or body['model']))
    _clean_tools(body)
    is_stream = bool(body.get('stream', False))
    url = f'{BLACKBOX_BASE}/chat/completions'
    if is_stream:
        status, resp, key = await proxy_request_with_pool('POST', url, body, request, is_stream=True)
        if status != 200:
            return JSONResponse(status_code=status, content=resp if isinstance(resp, dict) else {'error': {'message': str(resp), 'type': 'api_error'}})
        return StreamingResponse(stream_passthrough(resp, key), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
    status, data, _ = await proxy_request_with_pool('POST', url, body, request)
    if status != 200:
        # BB-15: record errors too so the error_rate metric can move.
        await metrics.record_request(model=body.get('model'), path='/v1/chat/completions', status_code=status)
        return JSONResponse(status_code=status, content=data if isinstance(data, dict) else {'error': {'message': str(data), 'type': 'api_error'}})
    if isinstance(data, dict) and data.get('type') == 'message' and 'content' in data:
        data = anthropic_to_openai_response(data, body.get('model', ''))
    await metrics.record_request(model=body.get('model'), path='/v1/chat/completions', prompt_tokens=(data.get('usage') or {}).get('prompt_tokens', 0), completion_tokens=(data.get('usage') or {}).get('completion_tokens', 0), status_code=status)
    return JSONResponse(_ensure_chat_message(data))


@app.post('/v1/responses')
async def responses(request: Request):
    _auth_check(request)
    # BB-11/DR-7: /v1/responses was the only POST inference endpoint without
    # the per-IP limiter.
    if not check_rate_limit(_client_ip(request)):
        return JSONResponse(status_code=429, content={"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    # BB-12/DR-8: contain all handler exceptions — return shaped 400/502
    # instead of a raw ASGI 500 (e.g. non-numeric max_output_tokens).
    try:
        requested = body.get('model')
        model = _normalize_model(requested) if requested else ''
        if requested is not None:
            body['model'] = model
        if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
            return JSONResponse(status_code=400, content=free_only_error(requested))
        if free_only_enabled() and model and not model_allowed(model):
            return JSONResponse(status_code=400, content=free_only_error(requested or model))
        # BUG-SEC-RESPONSE-STORE fix: extract principal for tenant-isolated store.
        principal = _extract_principal(request)
        chat_body = responses_to_chat(body, principal)
        chat_body['stream'] = bool(body.get('stream', False))
        # BB-12/DR-9 (BUG-SEC3): validate the translated body — positive-int
        # check and the 1,000,000 max_tokens overflow cap now cover
        # /v1/responses, not just the chat endpoints.
        err = _validate_chat_body(chat_body)
        if err:
            return JSONResponse(status_code=400, content=err)
        url = f'{BLACKBOX_BASE}/chat/completions'
        if chat_body['stream']:
            status, resp, key = await proxy_request_with_pool('POST', url, chat_body, request, is_stream=True)
            if status != 200:
                await metrics.record_request(model=model, path='/v1/responses', status_code=status)
                return JSONResponse(status_code=status, content=resp if isinstance(resp, dict) else {'error': {'message': str(resp), 'type': 'api_error'}})
            rid = f"resp_{int(time.time()*1000)}"
            return StreamingResponse(_responses_stream(resp, key, rid, model, chat_body, principal), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
        status, data, _ = await proxy_request_with_pool('POST', url, chat_body, request)
        if status != 200:
            await metrics.record_request(model=model, path='/v1/responses', status_code=status)
            return JSONResponse(status_code=status, content=data if isinstance(data, dict) else {'error': {'message': str(data), 'type': 'api_error'}})
        await metrics.record_request(model=model, path='/v1/responses', prompt_tokens=(data.get('usage') or {}).get('prompt_tokens', 0), completion_tokens=(data.get('usage') or {}).get('completion_tokens', 0), status_code=status)
        resp_obj = chat_to_responses(model, data)
        _store_response(principal, resp_obj.get('id'), chat_body.get('messages', []) + [_assistant_message_from_chat(data)])
        return JSONResponse(resp_obj)
    except HTTPException:
        raise
    except (ValueError, TypeError) as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid request: {e}'}})
    except Exception as e:
        logger.error(f'[responses] unhandled error: {e}')
        return JSONResponse(status_code=502, content={'error': {'type': 'api_error', 'message': f'Proxy error: {str(e)[:2000]}'}})


def _emit_response_event(seq_ref, etype, payload):
    seq_ref[0] += 1
    return f"event: {etype}\ndata: {json.dumps({'type': etype, 'sequence_number': seq_ref[0], **payload}, ensure_ascii=False)}\n\n"


async def _responses_stream(resp, key, rid: str, model: str, chat_body: dict, principal: str = ''):
    seq = [0]
    msg_id = 'msg-1'
    acc_text = ''
    acc_usage = None
    buffer = b''
    tool_accs = []
    next_output_index = 1
    rsn_started = False
    rsn_index = None
    rsn_id = f"rsn_{int(time.time()*1000)}"
    acc_reason = ""
    upstream_err: list[str] = []  # R-03: mid-stream upstream error frames

    def emit(etype, payload):
        return _emit_response_event(seq, etype, payload)

    def usage_obj():
        return acc_usage or {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}

    def get_tool_acc(tc):
        nonlocal next_output_index
        idx = tc.get('index') if isinstance(tc.get('index'), int) else len(tool_accs)
        acc = tool_accs[idx] if idx < len(tool_accs) else None
        if acc is None:
            acc = {'call_id': tc.get('id') or f"call_{idx}_{int(time.time()*1000)}", 'name': '', 'args': '', 'output_index': next_output_index, 'added': False}
            next_output_index += 1
            while len(tool_accs) <= idx:
                tool_accs.append(None)
            tool_accs[idx] = acc
        if tc.get('id'):
            acc['call_id'] = tc['id']
        return acc

    async def process_payload(payload: bytes):
        nonlocal acc_text, acc_usage, acc_reason, rsn_started, rsn_index, next_output_index
        # B-01 fix: an empty `data:` payload is a valid SSE keep-alive /
        # empty event, NOT end-of-stream (nous N-09 parity). Treating it as a
        # terminator ended turns mid-generation.
        if payload == b'':
            return
        if payload in (b'[DONE]', b'"[DONE]"'):
            return
        try:
            c = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return
        # R-03: an upstream {"error": ...} frame must NOT be dropped; record it
        # so the stream terminates with response.failed instead of a fabricated
        # response.completed carrying partial text.
        if isinstance(c, dict) and c.get('error') is not None and 'choices' not in c:
            _e = c['error']
            nonlocal_err = _e.get('message') if isinstance(_e, dict) else str(_e)
            logger.error(f'[responses] upstream error frame: {nonlocal_err}')
            upstream_err.append(str(nonlocal_err or 'upstream error'))
            return
        if c.get('usage'):
            u = c['usage']
            acc_usage = {'input_tokens': u.get('prompt_tokens', u.get('input_tokens', 0)) or 0, 'output_tokens': u.get('completion_tokens', u.get('output_tokens', 0)) or 0, 'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}
        d = ((c.get('choices') or [{}])[0].get('delta') or {})
        if d.get('content'):
            acc_text += d['content']
            yield emit('response.output_text.delta', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'delta': d['content']})
        # Reasoning (Blackbox reasoning_content) — MUST be streamed so the client
        # sees progress during thinking (Codex / OpenAI SDK aborts a silent
        # stream → "stops mid-way"). Mirror nvidia-python.
        reason_delta = d.get('reasoning_content') if isinstance(d.get('reasoning_content'), str) else (d.get('reasoning') if isinstance(d.get('reasoning'), str) else '')
        if reason_delta:
            if not rsn_started:
                rsn_started = True
                rsn_index = next_output_index
                next_output_index += 1
                yield emit('response.output_item.added', {'output_index': rsn_index, 'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress', 'summary': '', 'content': []}})
            acc_reason += reason_delta
            yield emit('response.reasoning_text.delta', {'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'delta': reason_delta})
        for tc in d.get('tool_calls') or []:
            acc = get_tool_acc(tc)
            fn = tc.get('function') or {}
            if not acc['added']:
                acc['added'] = True
                yield emit('response.output_item.added', {'output_index': acc['output_index'], 'item': {'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress', 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': ''}})
            if fn.get('name'):
                acc['name'] += fn['name']
                yield emit('response.function_call.delta', {'item_id': acc['call_id'], 'output_index': acc['output_index'], 'delta': fn['name'], 'name': acc['name']})
            if fn.get('arguments'):
                acc['args'] += fn['arguments']
                yield emit('response.function_call.delta', {'item_id': acc['call_id'], 'output_index': acc['output_index'], 'delta': fn['arguments']})

    failed = False
    last_hb = time.time()
    try:
        yield emit('response.created', {'response': {'id': rid, 'model': model, 'status': 'in_progress'}})
        yield emit('response.in_progress', {'response': {'id': rid, 'status': 'in_progress'}})
        yield emit('response.output_item.added', {'output_index': 0, 'item': {'id': msg_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []}})
        yield emit('response.content_part.added', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})
        # BB-5/DR-1: heartbeat while upstream is idle (this generator emits
        # whole SSE events, so comments are always line-aligned here).
        async for chunk in _iter_chunks_with_idle(resp, HEARTBEAT_MS / 1000.0):
            if chunk is _IDLE:
                now = time.time()
                if now - last_hb > (HEARTBEAT_MS / 1000.0):
                    yield ': heartbeat\n\n'
                    last_hb = now
                continue
            buffer += chunk
            if b'\r' in buffer:  # CRLF parity (nous N-08)
                buffer = buffer.replace(b'\r\n', b'\n')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.strip()
                if line.startswith(b'data:'):
                    async for out in process_payload(line[5:].strip()):
                        yield out
        tail = buffer.strip()
        if tail.startswith(b'data:'):
            async for out in process_payload(tail[5:].strip()):
                yield out
    except Exception as e:
        # B20 (transparency, severe): never fabricate model output text from a
        # transport error. Emit a proper response.failed terminal event so
        # clients treat it as an error (and can retry) instead of persisting
        # "[upstream stream error: …]" as a successful assistant answer.
        logger.error(f'[responses stream] {e}')
        failed = True
        yield emit('response.failed', {'response': {'id': rid, 'object': 'response', 'model': model, 'status': 'failed', 'error': {'code': 'upstream_error', 'message': f'upstream stream error: {str(e)[:2000]}'}}})
        yield 'data: [DONE]\n\n'
    finally:
        try:
            resp.release()
        except Exception:
            pass
        pool.release(key)

    if failed:
        return
    # R-03: the upstream reported a mid-stream failure. Terminate with
    # response.failed instead of fabricating a successful completion that the
    # client would persist as the assistant's answer.
    if upstream_err:
        yield emit('response.failed', {'response': {
            'id': rid, 'object': 'response', 'model': model, 'status': 'failed',
            'error': {'code': 'upstream_error', 'message': upstream_err[0][:2000]}}})
        yield 'data: [DONE]\n\n'
        return
    msg_item = {'id': msg_id, 'type': 'message', 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': acc_text, 'annotations': []}]}
    if rsn_started:
        yield emit('response.reasoning_text.done', {'item_id': rsn_id, 'output_index': rsn_index, 'content_index': 0, 'text': acc_reason})
        yield emit('response.output_item.done', {'output_index': rsn_index, 'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'completed', 'summary': '', 'text': acc_reason}})
    yield emit('response.output_text.done', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'text': acc_text})
    yield emit('response.content_part.done', {'item_id': msg_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': acc_text, 'annotations': []}})
    yield emit('response.output_item.done', {'output_index': 0, 'item': msg_item})
    outputs = [msg_item]
    if rsn_started:
        outputs.append({'id': rsn_id, 'type': 'reasoning', 'status': 'completed', 'summary': '', 'text': acc_reason})
    completed_tools = [a for a in tool_accs if a]
    for acc in completed_tools:
        fc_item = {'id': acc['call_id'], 'type': 'function_call', 'status': 'completed', 'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args']}
        yield emit('response.output_item.done', {'output_index': acc['output_index'], 'item': fc_item})
        outputs.append(fc_item)
    yield emit('response.completed', {'response': {'id': rid, 'object': 'response', 'created_at': int(time.time()), 'model': model, 'status': 'completed', 'output': outputs, 'usage': usage_obj()}})
    _store_response(principal, rid, list(chat_body.get('messages', [])) + [_assistant_message_from_chat({}, acc_text, completed_tools)])


_RESPONSE_STORE_MAX_ENTRIES = int(os.environ.get('RESPONSES_STORE_MAX_ENTRIES', '200'))
_RESPONSE_STORE_TTL_SEC = int(os.environ.get('RESPONSES_STORE_TTL_SEC', '3600'))
_RESPONSE_STORE_MAX_BYTES = int(os.environ.get('RESPONSES_STORE_MAX_BYTES', str(32 * 1024 * 1024)))


def _store_response(principal: str, rid: str, messages: list):
    """Store conversation history namespaced by principal (BUG-SEC-RESPONSE-STORE fix).

    B-33 fix: the previous implementation capped ENTRY COUNT only (200) with no
    TTL and no byte bound — 200 multi-MB histories is still unbounded in memory.
    Now bounded on all three axes (opencode/nous parity).
    """
    if not rid:
        return
    key = _response_store_key(principal, rid)
    try:
        size = len(json.dumps(messages, ensure_ascii=False))
    except (TypeError, ValueError):
        size = 0
    if size > _RESPONSE_STORE_MAX_BYTES:
        logger.warning(f'[responses] history for {rid} too large ({size}B); not stored')
        return
    _RESPONSE_STORE[key] = (time.time(), size, messages)
    _prune_response_store()


def _prune_response_store():
    """Evict expired, then oldest, entries until within all bounds (B-33)."""
    now = time.time()
    if _RESPONSE_STORE_TTL_SEC > 0:
        for k in [k for k, v in list(_RESPONSE_STORE.items())
                  if isinstance(v, tuple) and now - v[0] > _RESPONSE_STORE_TTL_SEC]:
            _RESPONSE_STORE.pop(k, None)
    while len(_RESPONSE_STORE) > _RESPONSE_STORE_MAX_ENTRIES:
        _RESPONSE_STORE.pop(next(iter(_RESPONSE_STORE)))
    total = sum(v[1] for v in _RESPONSE_STORE.values() if isinstance(v, tuple))
    while total > _RESPONSE_STORE_MAX_BYTES and len(_RESPONSE_STORE) > 1:
        _k, v = _RESPONSE_STORE.popitem()
        if isinstance(v, tuple):
            total -= v[1]


def _get_stored_conversation(principal: str, rid: str) -> list:
    """TTL-aware read from the bounded store (B-33)."""
    entry = _RESPONSE_STORE.get(_response_store_key(principal, rid))
    if not entry or not isinstance(entry, tuple):
        return []
    ts, _size, msgs = entry
    if _RESPONSE_STORE_TTL_SEC > 0 and (time.time() - ts) > _RESPONSE_STORE_TTL_SEC:
        _RESPONSE_STORE.pop(_response_store_key(principal, rid), None)
        return []
    return list(msgs)


@app.post('/v1/messages')
async def anthropic_messages(request: Request):
    _auth_check(request)
    if not check_rate_limit(_client_ip(request)):
        return JSONResponse(status_code=429, content={"type": "error", "error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    if not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens is required and must be a positive integer'}})
    sys_field = body.get('system')
    if sys_field is not None and not isinstance(sys_field, (str, list)):
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '"system" must be a string or array of content blocks'}})
    for t in body.get('tools', []) or []:
        if not isinstance(t.get('input_schema'), dict):
            return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tool.input_schema must be an object'}})
    requested = body.get('model')
    model = _normalize_model(requested) if requested else ''
    if requested is not None:
        body['model'] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(requested or model))
    # DR-8: contain handler exceptions — return a shaped Anthropic error
    # envelope instead of a raw ASGI 500.
    try:
        openai_body = anthropic_to_openai(body)
        openai_body['stream'] = bool(body.get('stream', False))
        # DR-9 parity (BUG-SEC3): apply the shared validation (incl. the
        # 1,000,000 max_tokens overflow cap) to the translated body.
        verr = _validate_chat_body(openai_body)
        if verr:
            return JSONResponse(status_code=400, content={'type': 'error', 'error': verr['error']})
        url = f'{BLACKBOX_BASE}/chat/completions'
        if openai_body['stream']:
            status, resp, key = await proxy_request_with_pool('POST', url, openai_body, request, is_stream=True)
            if status != 200:
                await metrics.record_request(model=model, path='/v1/messages', status_code=status)
                return JSONResponse(status_code=status, content={'type': 'error', 'error': {'type': 'api_error', 'message': str(resp)}})
            state = AnthropicStreamState(model)
            async def gen():
                last_hb = time.time()
                try:
                    for ev in state.start_events():
                        yield ev
                    buf = b''
                    # BB-5/DR-1: heartbeat while upstream is idle; this
                    # generator emits whole SSE events (line-aligned).
                    async for chunk in _iter_chunks_with_idle(resp, HEARTBEAT_MS / 1000.0):
                        if chunk is _IDLE:
                            now = time.time()
                            if now - last_hb > (HEARTBEAT_MS / 1000.0):
                                yield ': heartbeat\n\n'
                                last_hb = now
                            continue
                        buf += chunk
                        # CRLF parity (nous N-08).
                        if b'\r' in buf:
                            buf = buf.replace(b'\r\n', b'\n')
                        while b'\n' in buf:
                            line, buf = buf.split(b'\n', 1)
                            line = line.strip()
                            if not line.startswith(b'data:'):
                                continue
                            payload = line[5:].strip()
                            # B-01 fix: empty `data:` is a keep-alive, not EOF.
                            if payload == b'':
                                continue
                            if payload in (b'[DONE]', b'"[DONE]"'):
                                for ev in state.force_done():
                                    yield ev
                                return
                            try:
                                c = json.loads(payload)
                            except (json.JSONDecodeError, ValueError):
                                continue
                            for ev in state.translate_chunk(c):
                                yield ev
                    for ev in state.force_done():
                        yield ev
                except (GeneratorExit, asyncio.CancelledError):
                    # B-09 parity: no yields during generator finalization.
                    raise
                except Exception as e:
                    logger.error(f'[anthropic stream] {e}')
                    # B-07 fix: never fabricate a clean end_turn from a
                    # transport failure — the client would persist a truncated
                    # answer as successful and could not retry. Emit a real
                    # Anthropic error event first (nous N-05 parity).
                    try:
                        yield ('event: error\ndata: ' + json.dumps({
                            'type': 'error',
                            'error': {'type': 'api_error',
                                      'message': f'upstream stream error: {str(e)[:2000]}'},
                        }, ensure_ascii=False) + '\n\n')
                    except Exception:
                        pass
                    for ev in state.force_done():
                        yield ev
                finally:
                    try:
                        resp.release()
                    except Exception:
                        pass
                    pool.release(key)
            return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no'})
        status, data, _ = await proxy_request_with_pool('POST', url, openai_body, request)
        if status != 200:
            await metrics.record_request(model=model, path='/v1/messages', status_code=status)
            err = data.get('error', {}) if isinstance(data, dict) else {'message': str(data), 'type': 'api_error'}
            return JSONResponse(status_code=status, content={'type': 'error', 'error': {'type': err.get('type', 'api_error'), 'message': err.get('message', 'Unknown error')}})
        await metrics.record_request(model=model, path='/v1/messages', prompt_tokens=(data.get('usage') or {}).get('prompt_tokens', 0), completion_tokens=(data.get('usage') or {}).get('completion_tokens', 0), status_code=status)
        return JSONResponse(openai_to_anthropic(model, data))
    except HTTPException:
        raise
    except (ValueError, TypeError) as e:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid request: {e}'}})
    except Exception as e:
        logger.error(f'[messages] unhandled error: {e}')
        return JSONResponse(status_code=502, content={'type': 'error', 'error': {'type': 'api_error', 'message': f'Proxy error: {str(e)[:2000]}'}})


@app.get('/metrics')
async def get_metrics(request: Request):
    # Per project principle: dashboard must be fast/precise/accessible without
    # token. Metrics endpoints are PUBLIC so the dashboard can render live data.
    return await metrics.summary()


@app.get('/metrics/prom')
async def prom():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(pool.prom_metrics() + metrics.prom_metrics(), media_type='text/plain; version=0.0.4')


@app.get('/metrics/model-status')
async def model_status():
    # BB-9 companion: keep the synchronous SQLite scan off the event loop.
    return {
        'provider': 'blackbox',
        'catalog_age_sec': await asyncio.to_thread(MODEL_STORE.catalog_age_sec),
        'states': await asyncio.to_thread(MODEL_STORE.status_map),
    }



@app.get('/dashboard')
@app.get('/dashboard.html')
async def dashboard(request: Request):
    """Serve the wrapper dashboard HTML — PUBLIC (no auth required).

    Per project principle: dashboard is fast, precise, accessible without
    token. Security is NOT mandatory for the dashboard. The dashboard JS
    prompts for BEARER_TOKEN client-side only when calling auth-gated
    metrics endpoints.
    """
    from fastapi.responses import HTMLResponse
    dashboard_path = Path(__file__).parent.parent / "dashboard.html"
    if not dashboard_path.exists():
        return HTMLResponse(content="<html><body><h1>Dashboard not found</h1></body></html>")
    return HTMLResponse(content=dashboard_path.read_text())


# ── Catalog + MCP Integration (MUST BE BEFORE catch-all) ─────────────────
try:
    from common.catalog_integration import setup_catalog_routes, setup_mcp_server, free_only_enabled as _cfe
    setup_catalog_routes(app)
    setup_mcp_server(app, "blackbox")
    # Override free_only with shared version
    free_only_enabled = _cfe
    _HAS_CATALOG_INTEGRATION = True
except ImportError as _cie:
    _HAS_CATALOG_INTEGRATION = False
    pass


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Embeddings endpoint — not supported by this upstream provider.

    Returns a clear 501 so SDK clients get a structured error instead of
    a 404 catch-all. Operators who need embeddings should use nvidia-python
    or openrouter wrappers which DO support embeddings.
    """
    # B-31 fix: this endpoint previously parsed an arbitrary JSON body with NO
    # auth and NO rate limit — unauthenticated CPU/memory work reachable by
    # anyone who can hit the port. Gate it like every other POST surface.
    _auth_check(request)
    if not check_rate_limit(_client_ip(request)):
        return JSONResponse(status_code=429, content={"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        # B-19: validate the body is well-formed JSON (so clients get 400 not
        # 501 for malformed input) without binding an unused variable.
        await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return JSONResponse({
        "error": {
            "message": "Embeddings not supported by this provider. Use nvidia-python (port 9101) or openrouter (port 9106) for embeddings.",
            "type": "not_implemented_error",
            "code": 501,
        }
    }, status_code=501)


@app.api_route('/{path:path}', methods=['GET', 'POST'])
async def catch_all(path: str, request: Request):
    # Skip catalog/mcp paths - they have dedicated handlers
    if path.startswith("catalog/") or path.startswith("mcp/"):
        return JSONResponse(status_code=404, content={'error': {'message': f'Unknown endpoint: /{path}', 'type': 'invalid_request_error'}})
    return JSONResponse(status_code=404, content={'error': {'message': f'Unsupported: /{path}', 'type': 'not_found_error'}})


def main():
    import uvicorn
    uvicorn.run('src.main:app', host=BIND_HOST, port=LISTEN_PORT, log_level='info')


if __name__ == "__main__":
    main()