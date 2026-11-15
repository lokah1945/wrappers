#!/usr/bin/env python3
"""
wrapper-coarena v1.1.0 — FastAPI proxy for the Coarena Model API.

OpenAI + Anthropic compatible transparent proxy for Coarena
(https://coarena.ai — computer-use arena + free Model API by Coasty Systems).
Follows the standardized monorepo wrapper pattern and WRAPPER_CONTRACT v3.2.

Upstream (one key, two dialects — both served natively by Coarena):
  OpenAI-style base     https://coarena.ai/api/v1   (chat/completions,
                        responses, completions, models, limits, feedback)
  Anthropic-style base  https://coarena.ai/api/v1   (messages,
                        messages/count_tokens)

Production features (contract §2/§3/§4/§5/§6/§9/§10):
- Multi-key rotation + pacing + per-key cooldown (shared should_cooldown_key)
- Full streaming with sentinel-task heartbeat (common.sse) + native framing
- OpenAI Chat Completions + native Responses passthrough (auto Chat adapter
  only for a definitively unsupported route, owner-approved 2026-10-07)
- Anthropic Messages passthrough + cross-dialect translation (COMPATIBILITY_LAYER)
- Fail-closed auth middleware: Authorization: Bearer AND x-api-key
- .env hot reload, rich metrics (JSON snapshot persistence + Prometheus)
- Shared model registry outcome recording, off the request hot path
- Coarena extras: /v1/completions, /v1/limits, /v1/usage, /v1/feedback
"""

import asyncio
import copy
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

# ── Shared monorepo imports ──────────────────────────────────────────────
try:
    from common.model import (
        LocalModelRegistry,
        ModelRegistryClient,
        same_provider_model_id,
    )
    from common.model_state import (
        ModelStateStore,
        classify_upstream_error,
        credential_fingerprint,
    )
except ImportError:  # pragma: no cover - direct-script launch fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model import (
        LocalModelRegistry,
        ModelRegistryClient,
        same_provider_model_id,
    )
    from common.model_state import (
        ModelStateStore,
        classify_upstream_error,
        credential_fingerprint,
    )

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter
    _HAS_SIZE_LIMITER = True
except ImportError:  # pragma: no cover
    _HAS_SIZE_LIMITER = False

# B-08: shared sentinel-task idle iterator + CRLF normalisation.
from common.sse import (  # noqa: E402
    ClosingStreamingResponse as StreamingResponse,
    IDLE as _IDLE,
    iter_chunks_with_idle as _iter_chunks_with_idle,
)
from common.auth import extract_client_tokens, tokens_match, authenticated_client_token

from dotenv import load_dotenv

from .key_pool import KeyPool
from .metrics import Metrics

# ── Shared translations (§7: shared behaviour is never reimplemented) ────
try:
    from common.translations import (
        AnthropicStreamState,
        normalize_upstream_error as _normalize_upstream_error,
        parse_retry_after as _parse_retry_after,
        is_retriable_status as _is_retriable_status,
        should_cooldown_key as _should_cooldown_key,
        build_forward_headers as _build_forward_headers,
        sanitize_header_value,
        strip_cache_control as _strip_cache,
        anthropic_to_openai_response,
        openai_chat_to_anthropic_request,
    )
    from common.translations.think_tags import (
        extract_inline_thinking as _extract_inline_thinking,
        strip_trailing_think_tags as _strip_think_tags,
        thinking_signature as _thinking_signature,
    )
    from common.compat import (
        is_anthropic_upstream as _is_anthropic_upstream,
        responses_use_native_upstream as _responses_use_native_upstream,
        responses_chat_fallback_allowed as _responses_chat_fallback_allowed,
        validate_compat_layer,
        initialize_upstream_compatibility,
        translate_openai_chat_sse_to_responses as _translate_openai_chat_sse_to_responses,
        translate_anthropic_stream_to_openai_chat as _translate_anthropic_stream_to_openai_chat,
    )
except ImportError as _imp_err:  # pragma: no cover
    raise RuntimeError("common.translations import failed; wrapper requires shared translations") from _imp_err

# Final audit 2026-11-15: coarena previously had NO runtime reload path at all
# (dotenv was read at import, the pool only at lifespan startup). The shared
# .env hot-reload service in common/env_reload.py now provides add/remove/
# replace/zero-key hot swap without a restart.
_ENV_RELOAD_SERVICE = None


def start_env_watcher():
    """Start the shared .env hot-reload service (common/env_reload.py).

    WRAPPER_SKIP_DOTENV keeps test isolation; the check must stay first.
    """
    if os.environ.get('WRAPPER_SKIP_DOTENV', '').lower() in ('1', 'true', 'yes', 'on'):
        return
    global _ENV_RELOAD_SERVICE
    try:
        from common.env_reload import EnvReloadService, resolve_env_path, scan_pool_keys
        stop_env_watcher()

        async def _apply_env_snapshot(snapshot):
            # Runs on the service loop: schedule the state-preserving pool sync
            # there; never rebuild the pool from a watcher thread.
            try:
                new_keys = scan_pool_keys(snapshot, r'^COARENA_API_KEY(_\d+)?$')
                loop = asyncio.get_running_loop()
                asyncio.run_coroutine_threadsafe(pool.sync_keys(new_keys), loop)
                logger.info(f'[env] scheduled pool.sync_keys({len(new_keys)} keys)')
            except Exception as e:
                logger.warning(f'[env] key pool reload failed: {e}')

        service = EnvReloadService(
            env_path=resolve_env_path(ROOT / '.env'),
            apply=_apply_env_snapshot)
        if not service.initialize():
            logger.warning('[env] boot snapshot unavailable; ownership baseline starts empty')
        service.start()
        _ENV_RELOAD_SERVICE = service
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')


def stop_env_watcher():
    """Idempotently stop the shared .env reload service."""
    global _ENV_RELOAD_SERVICE
    service, _ENV_RELOAD_SERVICE = _ENV_RELOAD_SERVICE, None
    if service is not None:
        try:
            service.stop()
        except Exception as e:
            logger.warning(f'[env] watcher stop failed: {e}')

# ── Bootstrap ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
    # Final audit 2026-11-15: WRAPPER_ENV_FILE pins the boot .env to a
    # deterministic path (hot-swap acceptance harness uses it).
    _env_override = os.environ.get("WRAPPER_ENV_FILE", "").strip()
    if _env_override:
        load_dotenv(_env_override)
    else:
        load_dotenv(ROOT / '.env')
        load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────
LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/coarena/coarena.log')
try:
    from common.logging_utils import setup_logging
    logger = setup_logging('wrapper-coarena', log_file=LOG_FILE,
                           default_log_file='/tmp/wrapper-coarena.log',
                           log_format='%(asctime)s [coarena] %(message)s')
except ImportError:  # pragma: no cover
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        _log_file_handler = logging.FileHandler(LOG_FILE)
    except Exception:
        LOG_FILE = '/tmp/wrapper-coarena.log'
        _log_file_handler = logging.FileHandler(LOG_FILE)
    logger = logging.getLogger('wrapper-coarena')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [coarena] %(message)s',
                        handlers=[_log_file_handler, logging.StreamHandler()])

# ── Configuration ────────────────────────────────────────────────────────
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9107'))
BIND_HOST = os.environ.get('LISTEN_HOST', '127.0.0.1')
COARENA_BASE = os.environ.get('COARENA_BASE_URL', 'https://coarena.ai/api/v1').rstrip('/')
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(ROOT / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '300'))
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
VERSION = '1.1.0'
# Native Messages passthrough is the production default: the Coarena upstream
# serves the Anthropic Messages API natively. The isolated legacy adapter
# fixtures set NATIVE_MESSAGES_MODE=translate to exercise the converter.
NATIVE_MESSAGES_MODE = os.environ.get('NATIVE_MESSAGES_MODE', 'native').strip().lower()
if NATIVE_MESSAGES_MODE not in ('native', 'translate'):
    NATIVE_MESSAGES_MODE = 'native'
NATIVE_PROTOCOL_VALIDATION = os.environ.get('NATIVE_PROTOCOL_VALIDATION', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

MODEL_STORE = ModelStateStore('coarena', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('coarena', profile_db_path=MODEL_STATE_DB)
MODEL_REGISTRY_CLIENT = ModelRegistryClient()

# ── Validate Config (fail fast) ──────────────────────────────────────────

def validate_config():
    """Validate required configuration at startup.

    COMPATIBILITY_LAYER is operator-declared (1=OpenAI, 2=Anthropic, 3=Auto);
    an invalid value must never let the wrapper guess the upstream protocol.
    """
    try:
        validate_compat_layer()
    except ValueError as _e:
        print(f"❌ ERROR: {_e}")
        sys.exit(1)
    missing = []
    for var in ['COARENA_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var) and not os.environ.get('DISABLE_AUTH'):
            missing.append(var)
    if missing and not os.environ.get('DISABLE_AUTH'):
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    try:
        port = int(os.environ.get('LISTEN_PORT', '9107'))
        if not (1024 <= port <= 65535):
            print(f"❌ ERROR: Invalid port {port}")
            sys.exit(1)
    except ValueError:
        print("❌ ERROR: LISTEN_PORT must be an integer")
        sys.exit(1)


def _resolve_git_root():
    try:
        import subprocess
        return subprocess.check_output(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=os.path.dirname(os.path.abspath(__file__)), stderr=subprocess.DEVNULL, timeout=3
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
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=_resolve_git_root(),
                                       stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    except Exception:
        return 'unknown'


def _resolve_git_dirty():
    """True if the SOURCE tree differs from the reported commit at import time."""
    try:
        import subprocess
        out = subprocess.check_output(['git', 'status', '--porcelain', '--', '.', ':(exclude)runtime'],
                                      cwd=_resolve_git_root(),
                                      stderr=subprocess.DEVNULL, timeout=3).decode().strip()
        return bool(out)
    except Exception:
        return None


GIT_COMMIT = _resolve_git_commit()
GIT_DIRTY = _resolve_git_dirty()
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_only_enabled() -> bool:
    """FREE_ONLY=yes|true|1 → restrict to allowlisted models.

    Every Coarena Model API model is served on the free funded tier
    ($5/day, Coarena pays), so without an explicit allowlist every model
    qualifies. Operators may pin a subset via FREE_MODEL_ALLOWLIST.
    """
    v = (os.environ.get('FREE_ONLY') or 'no').strip().lower()
    return v in ('yes', 'true', '1', 'on', 'y')


def _free_model_allowlist() -> set:
    raw = (os.environ.get('FREE_MODEL_ALLOWLIST')
           or os.environ.get('COARENA_FREE_MODEL_ALLOWLIST') or '')
    return {m.strip() for m in raw.split(',') if m.strip()}


def is_free_model(model_id: str) -> bool:
    """Allowlist-bound free-model test; empty allowlist means every model."""
    allow = _free_model_allowlist()
    if not allow:
        return True  # upstream tier is free by design
    return str(model_id or '') in allow


def _bearer_token() -> str:
    return (os.environ.get('BEARER_TOKEN') or '').strip()


def _client_ip(request: Request) -> str:
    """Use the ASGI peer; forwarded headers require server-level trust."""
    host = getattr(request.client, 'host', None) if request.client else None
    return host or 'unknown'


# ── Rate Limiting ────────────────────────────────────────────────────────
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
        if now - _rate_limit_last_sweep > 300:
            _rate_limit_last_sweep = now
            for k in list(_rate_limit_store.keys()):
                _rate_limit_store[k] = [t for t in _rate_limit_store[k] if now - t < 60]
                if not _rate_limit_store[k]:
                    del _rate_limit_store[k]
        window = [t for t in _rate_limit_store.get(client_ip, []) if now - t < 60]
        if len(window) >= RATE_LIMIT_RPM:
            return False
        _rate_limit_store[client_ip].append(now)
        return True


def _error_response(content, status_code: int = 500, headers: dict | None = None) -> JSONResponse:
    """Shaped error response that also counts the error in metrics (B-39)."""
    try:
        metrics.record_error(status_code=status_code)
    except Exception:
        pass
    if headers is not None:
        return JSONResponse(content, status_code=status_code, headers=headers)
    return JSONResponse(content, status_code=status_code)


# ── Pool + metrics singletons (constructed before first request) ─────────
pool = KeyPool()
metrics = Metrics()


# ── Async HTTP session (§6.2: one shared session, never per request) ─────
_agent: aiohttp.ClientSession | None = None


async def get_agent() -> aiohttp.ClientSession:
    global _agent
    if _agent is None or _agent.closed:
        connector = aiohttp.TCPConnector(
            limit=int(os.environ.get('MAX_CONNECTIONS', '200')),
            limit_per_host=int(os.environ.get('MAX_CONNECTIONS_PER_HOST', '100')),
            ttl_dns_cache=300,
        )
        timeout = aiohttp.ClientTimeout(
            total=int(os.environ.get('REQUEST_TIMEOUT_SEC', '600')),
            connect=int(os.environ.get('CONNECT_TIMEOUT_SEC', '30')),
        )
        _agent = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _agent


# ── Background tasks (B-35: retained strong references) ──────────────────
_BG_TASKS: set = set()


def _spawn_background(coro, label: str = 'bg'):
    """Fire-and-forget with a retained strong reference + error logging."""
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)

    def _done(t: asyncio.Task):
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning('[bg:%s] background task failed: %r', label, exc)

    task.add_done_callback(_done)
    return task


# Alias used by isolated test sandboxes (tests inject their own discarder).
_fire_and_forget = _spawn_background


async def _record_model_result(model_id: str, api_key: str, status: int, data, url: str):
    """Persist the account-scoped upstream outcome (contract §10).

    Runs as a background task so the SQLite commit never sits on the TTFB
    path — persistence is never awaited before responding.
    """
    try:
        if status == 200:
            stored = await MODEL_STORE.record_status_async(
                model_id, credential_fingerprint(api_key), 'available', status, 'OK', endpoint=url)
        else:
            stored = await MODEL_STORE.record_error_async(model_id, api_key, status, data, endpoint=url)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            'coarena', model_id,
            stored.get('account_scope', credential_fingerprint(api_key)),
            stored.get('state', 'unknown'), status, stored.get('reason_code', ''),
            stored.get('reason_detail', ''), url,
        )
    except Exception as e:
        logger.warning(f'[model-state] coarena result record failed: {e}')


async def _drain_background_tasks(timeout: float = 5.0):
    """Await outstanding background tasks during shutdown (B-35)."""
    if not _BG_TASKS:
        return
    pending = list(_BG_TASKS)
    logger.info('[coarena] draining %d background task(s)', len(pending))
    try:
        await asyncio.wait(pending, timeout=timeout)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning('[coarena] background drain error: %r', e)


# ── Lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup and graceful shutdown (B-34)."""
    await initialize_upstream_compatibility(COARENA_BASE)
    logger.info("[coarena] Starting wrapper-coarena")
    pool.load_from_env()
    # Final audit 2026-11-15: start the shared .env hot-reload service after
    # the boot pool load so add/remove/replace/zero-key changes apply without
    # a restart (previously coarena had no runtime reload path at all).
    start_env_watcher()
    logger.info(f"[coarena] Ready on {BIND_HOST}:{LISTEN_PORT}")

    yield

    logger.info("[coarena] Starting graceful shutdown...")
    # Final audit 2026-11-15: stop the .env reload watcher before draining so
    # no reload can race pool/session teardown.
    stop_env_watcher()
    shutdown_start = time.time()
    max_wait = int(os.environ.get('SHUTDOWN_DRAIN_SEC', '30'))
    while shutdown_start + max_wait > time.time():
        total = sum(k.in_flight for k in pool.keys)
        if total == 0:
            logger.info("[coarena] All requests drained")
            break
        await asyncio.sleep(0.1)
    else:
        logger.warning("[coarena] Drain timeout — %d request(s) still in flight",
                       sum(k.in_flight for k in pool.keys))

    await _drain_background_tasks()
    await metrics.close()
    await MODEL_REGISTRY_CLIENT.stop()
    if _agent and not _agent.closed:
        await _agent.close()
    logger.info("[coarena] Shutdown complete")


# ── FastAPI App ──────────────────────────────────────────────────────────

app = FastAPI(
    title="wrapper-coarena",
    version=VERSION,
    description="OpenAI + Anthropic compatible proxy for the Coarena Model API",
    lifespan=lifespan,
)

# CORS — restrict to localhost for safety (operators can override via ALLOWED_ORIGINS)
_allowed_origins = os.environ.get('ALLOWED_ORIGINS', '').strip()
if _allowed_origins:
    _cors_origins = [o.strip() for o in _allowed_origins.split(',') if o.strip()]
else:
    _cors_origins = ['http://127.0.0.1', 'http://localhost', 'http://[::1]']
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id", "x-process-time"],
)

# R-01 fix: reject non-object JSON bodies with a shaped 400 instead of letting
# `body.get(...)` raise AttributeError -> HTTP 500 (common/body_guard.py).
try:
    from common.body_guard import JSONBodyGuard as _JSONBodyGuard
    app.add_middleware(_JSONBodyGuard)
except ImportError:  # pragma: no cover
    pass

if _HAS_SIZE_LIMITER:
    # §4: 10 MB request cap, same default as every sibling wrapper.
    app.add_middleware(RequestSizeLimiter)


# ── Auth Middleware (§5) ─────────────────────────────────────────────────

DISABLE_AUTH = os.environ.get('DISABLE_AUTH', '').strip().lower() in ('1', 'true', 'yes')
# B-28 fix: fail CLOSED when no bearer token is configured.
REQUIRE_AUTH = os.environ.get('REQUIRE_AUTH', 'true').strip().lower() not in ('0', 'false', 'no', 'off')

# B-27 fix: exact-match public paths + explicit method gating.
PUBLIC_PATHS_ANY = frozenset({
    '/health', '/ready', '/metrics', '/metrics/prom', '/dashboard', '/stats',
})
# Public discovery/telemetry — GET only. Every POST surface stays
# authenticated, including the coarena extras (B-31 parity).
PUBLIC_PATHS_GET = frozenset({
    '/api/tags', '/v1/models', '/v1/capabilities', '/version',
    '/metrics/models', '/metrics/keys', '/metrics/activity',
    '/metrics/chart/hourly', '/metrics/model-status',
})


def _is_public_path(path: str, method: str) -> bool:
    """Exact-match public-path test (B-27)."""
    if path in PUBLIC_PATHS_ANY:
        return True
    if method == 'GET' and (path in PUBLIC_PATHS_GET or path.startswith('/v1/models/')):
        return True
    return False


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Exempt OPTIONS preflight from auth so browser SDKs can CORS-negotiate.
    if request.method == 'OPTIONS':
        return await call_next(request)

    path = request.url.path
    method = request.method
    if not DISABLE_AUTH and not _is_public_path(path, method):
        token = _bearer_token()
        # B-28 fix: fail CLOSED when no token is configured.
        if not token:
            if REQUIRE_AUTH:
                logger.error('[auth] BEARER_TOKEN unset and REQUIRE_AUTH=true — refusing request to %s', path)
                return _error_response(
                    {"error": {"message": "Server auth not configured", "type": "authentication_error"}},
                    status_code=503,
                )
            logger.warning('[auth] token unset and REQUIRE_AUTH=false — serving %s OPEN (insecure)', path)
        else:
            # B-30 parity: compare as bytes so a non-ASCII token yields a
            # clean 401 instead of a TypeError -> 500. Accepts both
            # Authorization: Bearer and x-api-key (Anthropic SDK/Claude Code).
            ok = any(tokens_match(candidate, token)
                     for candidate in extract_client_tokens(request.headers))
            if not ok:
                return _error_response(
                    {"error": {"message": "Unauthorized", "type": "authentication_error"}},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer'},
                )

    # Rate limit
    ip = _client_ip(request)
    if not check_rate_limit(ip):
        return _error_response(
            {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # Process + correlation
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start
    rid = request.headers.get('x-request-id', str(uuid.uuid4()))
    response.headers['x-request-id'] = rid
    response.headers['x-process-time'] = f"{elapsed:.3f}"
    return response


# ── Usage sniffing (DASH fix: tokens counted without touching the stream) ─

def _usage_tokens(data) -> tuple:
    """(prompt, completion) from an OpenAI/Anthropic/Responses usage block."""
    if not isinstance(data, dict):
        return 0, 0
    usage = data.get('usage')
    if not isinstance(usage, dict) and isinstance(data.get('message'), dict):
        usage = data['message'].get('usage')
    if not isinstance(usage, dict) and isinstance(data.get('response'), dict):
        usage = data['response'].get('usage')
    if not isinstance(usage, dict):
        return 0, 0
    try:
        prompt = int(usage.get('prompt_tokens', usage.get('input_tokens', 0)) or 0)
        completion = int(usage.get('completion_tokens', usage.get('output_tokens', 0)) or 0)
        return prompt, completion
    except (TypeError, ValueError):
        return 0, 0


async def _sniff_stream_usage(source, model: str, key_label: str, started_at: float | None = None):
    """Yield frames UNCHANGED while extracting token usage from SSE frames."""
    buffer = b''
    in_tok = 0
    out_tok = 0
    try:
        async for frame in source:
            if isinstance(frame, (bytes, bytearray)):
                buffer += bytes(frame)
            elif isinstance(frame, str):
                buffer += frame.encode('utf-8', 'replace')
            else:
                yield frame
                continue
            yield frame
            while b'\n' in buffer:
                line_bytes, buffer = buffer.split(b'\n', 1)
                line = line_bytes.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                data_str = line[5:].strip()
                if not data_str or data_str == '[DONE]':
                    continue
                try:
                    data = json.loads(data_str)
                except (ValueError, TypeError):
                    continue
                if not isinstance(data, dict):
                    continue
                usage = data.get('usage')
                message = data.get('message')
                if not isinstance(usage, dict) and isinstance(message, dict) and isinstance(message.get('usage'), dict):
                    usage = message['usage']
                if not isinstance(usage, dict):
                    resp_obj = data.get('response')
                    if isinstance(resp_obj, dict) and isinstance(resp_obj.get('usage'), dict):
                        usage = resp_obj['usage']
                if isinstance(usage, dict):
                    try:
                        in_tok = max(in_tok, int(usage.get('input_tokens', 0) or 0),
                                     int(usage.get('prompt_tokens', 0) or 0))
                        out_tok = max(out_tok, int(usage.get('output_tokens', 0) or 0),
                                      int(usage.get('completion_tokens', 0) or 0))
                    except (TypeError, ValueError):
                        pass
    finally:
        try:
            _spawn_background(
                metrics.record_tokens(model=model, key_label=key_label,
                                      prompt_tokens=in_tok, completion_tokens=out_tok,
                                      latency_ms=(
                                          (time.perf_counter() - started_at) * 1000.0
                                          if started_at is not None else None)),
                'metrics-stream-usage')
        except Exception:
            pass
        # Close the wrapped upstream generator SYNCHRONOUSLY so the credential
        # and socket are released within the close call itself (R-07).
        try:
            _close = getattr(source, 'aclose', None)
            if _close is not None:
                await _close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# PROXY CORE — multi-key retry loop
# ══════════════════════════════════════════════════════════════════════════

async def _proxy_request(*args, path: str | None = None, body: dict | None = None,
                         headers: dict | None = None, stream: bool = False,
                         request: Request | None = None,
                         terminal_done: bool = False,
                         native_stream: bool = False,
                         recover_context: bool = True) -> Response:
    """Generic proxy handler for the Coarena API with a multi-key retry loop.

    Positional contract: (method, upstream_path, body) — body stays the third
    positional argument. `path`/`body` may also be supplied by keyword.
    Iterates over all available keys on retriable failures (401/402/403/408/
    409/429/5xx). Returns 429 (not 503) when all keys are exhausted so SDKs
    auto-retry.

    terminal_done: optional legacy OpenAI stream finalization. Native
    matching dialects preserve every upstream frame and terminal verbatim.
    """
    from common.context_recovery import context_request, ContextRecoveryState
    from common.http_headers import upstream_response_headers
    context_state = ContextRecoveryState(enabled=recover_context)
    method = args[0] if args else 'POST'
    upstream_path = path if path is not None else (args[1] if len(args) > 1 else '')
    if body is None:
        body = args[2] if len(args) > 2 else None
    model_id = (body or {}).get("model", "") if body else ""
    endpoint = upstream_path.strip('/').rsplit('/', 1)[-1]

    # Call-plan validation BEFORE spending an upstream request so a mutated or
    # invalid model id cannot burn a key's quota (NO MODEL FALLBACK contract).
    if model_id:
        surface = ('anthropic_messages' if endpoint == 'messages'
                   else ('openai_responses' if endpoint == 'responses' else 'openai_chat'))
        try:
            call_plan = MODEL_REGISTRY.call_plan(model_id, surface)
            if not same_provider_model_id('coarena', call_plan.model.provider_model_id, model_id):
                return _error_response({"error": {
                    "type": "server_error",
                    "message": "Model identity changed during call-plan resolution",
                    "code": "MODEL_ID_MUTATION"}}, status_code=500)
        except ValueError as exc:
            return _error_response({"error": {
                "type": "invalid_request_error", "message": str(exc),
                "code": "MODEL_CALL_PLAN_INVALID"}}, status_code=400)
        except Exception as exc:  # registry unavailable — do not block traffic
            logger.debug(f'[coarena] call-plan check skipped: {exc}')

    attempts = max(1, pool.total_keys)
    last_status = 429
    last_data = {"error": {"message": "All keys exhausted or rate-limited", "type": "rate_limit_error"}}
    last_headers = {}

    attempted_keys: set[str] = set()
    for _ in range(attempts):
        acq = await pool.acquire(model=model_id, exclude=attempted_keys)
        if not acq:
            break
        key_obj = acq['key']
        attempted_keys.add(key_obj.api_key)
        key_released = False  # I4: exactly-once release guard
        attempt_started = time.perf_counter()

        url = f"{COARENA_BASE}/{upstream_path.lstrip('/')}" if upstream_path else COARENA_BASE

        # Transparent proxy: swap only the credential, forward everything else.
        fwd = {
            "Authorization": f"Bearer {key_obj.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if request is not None:
            forwarded = _build_forward_headers(request.headers)
            for k, v in forwarded.items():
                fwd[k] = v
        if headers:
            fwd.update(headers)

        try:
            agent = await get_agent()
            timeout = aiohttp.ClientTimeout(
                total=None if stream else int(os.environ.get('REQUEST_TIMEOUT_SEC', '600')),
                sock_connect=int(os.environ.get('CONNECT_TIMEOUT_SEC', '30')),
                sock_read=int(os.environ.get('STREAM_SOCK_READ_TIMEOUT_SEC', '300')),
            )

            def _send(payload):
                if method.upper() == 'GET':
                    return agent.get(url, headers=fwd, timeout=timeout)
                return agent.request(method, url, json=payload, headers=fwd, timeout=timeout)

            if stream:
                # Streaming: do NOT async-with so caller owns release.
                resp = await context_request(
                    _send, body, state=context_state, store=globals().get('MODEL_STORE'),
                    credential=key_obj.api_key, endpoint=url)
                if resp.status >= 400:
                    last_headers = upstream_response_headers(resp)
                    try:
                        error_text = await resp.text()
                        try:
                            last_data = json.loads(error_text)
                        except Exception:
                            last_data = {"error": {"message": error_text[:2000], "type": "upstream_error",
                                                     "status": resp.status}}
                    finally:
                        resp.release()
                    if endpoint == 'responses' and _responses_chat_fallback_allowed(resp.status, last_data):
                        return JSONResponse(last_data, status_code=resp.status, headers=last_headers)
                    retry_after = _parse_retry_after(resp.headers, last_data) if resp.status == 429 else None
                    # §2.2.3: cooldown decisions come from the shared helper —
                    # a model-capacity error never cools the credential.
                    if _should_cooldown_key(resp.status, last_data):
                        pool.mark_failure(key_obj, status_code=resp.status,
                                          available_keys=pool.available_keys,
                                          model=model_id, retry_after=retry_after)
                    pool.release(key_obj)
                    key_released = True  # I4: mark released, skip outer finally
                    _spawn_background(metrics.record_request(model=model_id, status_code=resp.status,
                                                              key_label=getattr(key_obj, 'label', ''),
                                                              latency_ms=(time.perf_counter() - attempt_started) * 1000.0,
                                                              path=upstream_path),
                                      'metrics-stream-err')
                    if model_id:
                        _spawn_background(_record_model_result(model_id, key_obj.api_key,
                                                               resp.status, last_data, url),
                                          'model-state')
                    last_status = resp.status
                    if _is_retriable_status(resp.status) or classify_upstream_error(resp.status, last_data).retry_same_model:
                        continue  # retry with next key
                    return JSONResponse(last_data, status_code=resp.status, headers=last_headers)
                if hasattr(pool, 'mark_success'):
                    pool.mark_success(key_obj, available_keys=pool.available_keys)
                # B-39: streaming requests are counted at stream start.
                _spawn_background(metrics.record_request(model=model_id, status_code=200,
                                                          key_label=getattr(key_obj, 'label', ''),
                                                          streaming=True, path=upstream_path),
                                  'metrics-stream')
                if model_id:
                    _spawn_background(_record_model_result(model_id, key_obj.api_key, 200, None, url),
                                      'model-state')

                resp_ref = resp
                released = False
                key_released = True  # I4: stream path owns release; outer finally skips

                def release_stream():
                    nonlocal released
                    if not released:
                        released = True
                        try:
                            resp_ref.release()
                        finally:
                            pool.release(key_obj)

                async def stream_gen():
                    nonlocal released
                    # R-06: track whether upstream already terminated. Native
                    # framing is preserved verbatim; [DONE] synthesis happens
                    # only when terminal_done is requested (legacy adapters).
                    saw_done = False
                    ends_newline = True
                    # §3.5: close the wrapped upstream iterator
                    # deterministically instead of leaving it to the asyncgen
                    # GC finalizer (client disconnect must release the
                    # upstream read immediately, not "eventually").
                    content_iter = resp_ref.content
                    try:
                        _aiter = getattr(content_iter, '__aiter__', None)
                        if _aiter is not None:
                            content_iter = _aiter()
                        async for line in content_iter:
                            if line:
                                if b'[DONE]' in line:
                                    saw_done = True
                                ends_newline = line.endswith(b'\n')
                            yield line
                        if terminal_done:
                            if not saw_done:
                                if not ends_newline:
                                    yield b'\n\n'
                                yield b'data: [DONE]\n\n'
                            elif not ends_newline:
                                yield b'\n\n'
                    except asyncio.CancelledError:
                        raise
                    finally:
                        release_stream()
                        close_iter = getattr(content_iter, 'aclose', None)
                        if callable(close_iter) and content_iter is not resp_ref.content:
                            try:
                                await close_iter()
                            except Exception:
                                pass

                if native_stream:
                    return StreamingResponse(
                        _sniff_stream_usage(stream_gen(), model_id, getattr(key_obj, 'label', ''), started_at=attempt_started),
                        on_close=release_stream, status_code=resp.status,
                        media_type="text/event-stream",
                        headers={**upstream_response_headers(resp)})

                async def stream_with_heartbeat():
                    # B-08: sentinel-task idle detection via common.sse —
                    # asyncio.wait_for is forbidden (it cancels the pending
                    # read and heartbeats a dead upstream forever).
                    last_hb = time.time()
                    at_event_boundary = True
                    framing_tail = b''
                    hb_interval = float(HEARTBEAT_MS) / 1000.0
                    inner = _sniff_stream_usage(
                        stream_gen(), model_id, getattr(key_obj, 'label', ''), started_at=attempt_started)
                    try:
                        async for chunk in _iter_chunks_with_idle(inner, hb_interval):
                            now = time.time()
                            if chunk is _IDLE:
                                if at_event_boundary and (now - last_hb) > hb_interval:
                                    yield b': heartbeat\n\n'
                                    last_hb = now
                                continue
                            yield chunk
                            if chunk:
                                raw_chunk = bytes(chunk) if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode()
                                framing_tail = (framing_tail + raw_chunk)[-4:]
                                at_event_boundary = framing_tail.endswith((b'\n\n', b'\r\n\r\n'))
                            last_hb = time.time()
                    except (GeneratorExit, asyncio.CancelledError):
                        raise
                    except Exception as e:
                        # B-08: a real upstream failure must terminate the
                        # stream visibly rather than being masked as idle.
                        logger.warning(f'[coarena] upstream stream error ({type(e).__name__}: {e})')
                        raise
                    finally:
                        try:
                            await inner.aclose()
                        except Exception:
                            pass

                return StreamingResponse(
                    stream_with_heartbeat(),
                    on_close=release_stream,
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={
                        "cache-control": "no-cache", "x-accel-buffering": "no",
                        **upstream_response_headers(resp),
                    },
                )

            # Non-streaming
            async with context_request(
                _send, body, state=context_state, store=globals().get('MODEL_STORE'),
                credential=key_obj.api_key, endpoint=url) as resp:
                response_headers = upstream_response_headers(resp)
                await metrics.record_request(model=model_id, status_code=resp.status,
                                             key_label=getattr(key_obj, 'label', ''),
                                             latency_ms=(
                                                 (time.perf_counter() - attempt_started) * 1000.0
                                                 if resp.status >= 400 else None),
                                             path=upstream_path)
                text = await resp.text()
                if model_id:
                    _spawn_background(
                        _record_model_result(model_id, key_obj.api_key, resp.status,
                                             None if resp.status == 200 else text, url),
                        'model-state')
                if resp.status >= 400:
                    try:
                        body_data = json.loads(text) if text else {}
                    except Exception:
                        body_data = {"error": {"message": text[:2000], "type": "upstream_error",
                                           "status": resp.status}}
                    if endpoint == 'responses' and _responses_chat_fallback_allowed(resp.status, body_data):
                        return JSONResponse(body_data, status_code=resp.status, headers=response_headers)
                    retry_after = _parse_retry_after(resp.headers, body_data if isinstance(body_data, dict) else None) if resp.status == 429 else None
                    if _should_cooldown_key(resp.status, body_data):
                        pool.mark_failure(key_obj, status_code=resp.status,
                                          available_keys=pool.available_keys,
                                          model=model_id,
                                          retry_after=retry_after)
                    last_status = resp.status
                    last_data = body_data
                    last_headers = response_headers
                    if _is_retriable_status(resp.status) or classify_upstream_error(resp.status, body_data).retry_same_model:
                        continue  # retry with next key
                    return JSONResponse(body_data, status_code=resp.status, headers=response_headers)
                if hasattr(pool, 'mark_success'):
                    pool.mark_success(key_obj, available_keys=pool.available_keys)
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                _tok = _usage_tokens(data) if resp.status == 200 else (0, 0)
                _spawn_background(
                    metrics.record_tokens(model=model_id,
                                           key_label=getattr(key_obj, 'label', ''),
                                           prompt_tokens=_tok[0], completion_tokens=_tok[1],
                                           latency_ms=(time.perf_counter() - attempt_started) * 1000.0),
                    'metrics-usage')
                return JSONResponse(content=data, status_code=resp.status, headers=response_headers)

        except asyncio.TimeoutError:
            pool.mark_failure(key_obj, reason="timeout", available_keys=pool.available_keys)
            last_status = 504
            last_data = {"error": {"message": "Upstream request timed out", "type": "timeout_error"}}
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pool.mark_failure(key_obj, reason=str(e)[:100], available_keys=pool.available_keys)
            logger.error(f"[coarena] Proxy error: {e}")
            last_status = 502
            last_data = {"error": {"message": f"Upstream connection error: {type(e).__name__}: {str(e)[:500]}",
                                    "type": "api_error"}}
            continue
        finally:
            # I4: exactly-once release — skip if key already released.
            if not key_released:
                try:
                    pool.release(key_obj)
                except Exception:
                    pass
                key_released = True

    # All keys exhausted — return 429 (not 503) so SDKs auto-retry with backoff.
    retry_after = str(int(os.environ.get('KEY_EXHAUSTED_RETRY_AFTER', '30')))
    return JSONResponse(
        last_data if isinstance(last_data, dict) else {"error": {"message": str(last_data)[:2000], "type": "rate_limit_error"}},
        status_code=last_status if last_status in (429, 401, 402, 403, 408, 409) else 429,
        headers={**last_headers, "retry-after": last_headers.get('retry-after', retry_after)},
    )


def _check_free_only(model: str) -> JSONResponse | None:
    """Check FREE_ONLY constraint. Returns error response if blocked."""
    if free_only_enabled() and model and not is_free_model(model):
        return _error_response(
            {"error": {"message": f"FREE_ONLY mode: model '{model}' is not in FREE_MODEL_ALLOWLIST.",
                       "type": "invalid_request_error"}},
            status_code=400,
        )
    return None


# ══════════════════════════════════════════════════════════════════════════
# PROXY ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.post("/v1/completions")
async def completions(request: Request):
    """Native Coarena legacy Completions passthrough (upstream-native route)."""
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    blocked = _check_free_only(body.get("model", ""))
    if blocked:
        return blocked
    stream = bool(body.get("stream", False))
    return await _proxy_request("POST", "completions", body, path="completions",
                                stream=stream, request=request,
                                native_stream=stream, recover_context=True)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    from common.context_recovery import context_response_headers
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # §4: local validation on the cross-dialect adapter or when explicitly
    # opted in; the native OpenAI-compatible upstream stays authoritative.
    mt = body.get("max_tokens")
    if globals().get('NATIVE_PROTOCOL_VALIDATION', False) or _is_anthropic_upstream():
        if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0):
            return _error_response({"error": {"message": "max_tokens must be a positive integer",
                                              "type": "invalid_request_error"}}, status_code=400)
        if isinstance(mt, int) and mt > 1_000_000:
            return _error_response({"error": {"message": "max_tokens exceeds maximum allowed value of 1000000",
                                              "type": "invalid_request_error"}}, status_code=400)
    # §4: unknown roles and orphan tool messages are rejected on the adapter.
    cross_dialect = _is_anthropic_upstream()
    for _m in body.get("messages") or []:
        if cross_dialect and isinstance(_m, dict) and _m.get("role") not in (None, "system", "user", "assistant", "tool", "developer", "function"):
            return _error_response({"error": {"message": f"Invalid role: {_m.get('role')!r}",
                                              "type": "invalid_request_error"}}, status_code=400)
        if isinstance(_m, dict) and _m.get("role") == "tool" and not _m.get("tool_call_id"):
            return _error_response({"error": {"message": "tool role requires tool_call_id",
                                              "type": "invalid_request_error"}}, status_code=400)

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    stream = body.get("stream", False)

    # COMPATIBILITY_LAYER=2: upstream declared Anthropic — translate the OpenAI
    # chat request once and translate the Anthropic response back.
    if _is_anthropic_upstream():
        anthro_body = openai_chat_to_anthropic_request(body)
        anthro_body["stream"] = stream
        res = await _proxy_request("POST", "messages", anthro_body, path="messages",
                                   stream=stream, request=request, terminal_done=False)
        if isinstance(res, StreamingResponse):
            return StreamingResponse(
                _translate_anthropic_stream_to_openai_chat(
                    res.body_iterator, model, float(HEARTBEAT_MS) / 1000.0),
                on_close=res.aclose,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": res.headers.get("x-request-id", ""),
                },
            )
        if isinstance(res, JSONResponse):
            try:
                payload = json.loads(res.body)
            except Exception:
                return res
            if isinstance(payload, dict) and payload.get('error'):
                return res
            if isinstance(payload, dict) and payload.get("type") == "message" and "content" in payload:
                oai_resp = anthropic_to_openai_response(payload, model)
                return JSONResponse(oai_resp, status_code=res.status_code)
        return res

    return await _proxy_request("POST", "chat/completions", body, path="chat/completions",
                                stream=stream, request=request,
                                native_stream=bool(stream), recover_context=True)


@app.post("/v1/responses")
async def responses(request: Request):
    """Native Coarena Responses, cross-Anthropic, or the owner-approved Chat adapter."""
    from common.context_recovery import context_response_headers
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    if globals().get('NATIVE_PROTOCOL_VALIDATION', False) or not _responses_use_native_upstream():
        for _mt_key in ("max_output_tokens", "max_tokens"):
            _mtv = body.get(_mt_key)
            if _mtv is not None and (not isinstance(_mtv, int) or isinstance(_mtv, bool) or _mtv <= 0):
                return _error_response({"error": {"message": f"{_mt_key} must be a positive integer",
                                                  "type": "invalid_request_error"}}, status_code=400)
            if isinstance(_mtv, int) and _mtv > 1_000_000:
                return _error_response({"error": {"message": f"{_mt_key} exceeds maximum allowed value of 1000000",
                                                  "type": "invalid_request_error"}}, status_code=400)

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    if _responses_use_native_upstream():
        native_response = await _proxy_request('POST', 'responses', body, path='responses',
                                               stream=bool(body.get('stream', False)), request=request,
                                               native_stream=bool(body.get('stream', False)),
                                               recover_context=True)
        if not isinstance(native_response, JSONResponse):
            return native_response
        try:
            native_payload = json.loads(native_response.body)
        except Exception:
            return native_response
        if not _responses_chat_fallback_allowed(native_response.status_code, native_payload):
            return native_response
        previous_id = body.get('previous_response_id')
        if previous_id is not None:
            fallback_principal = _request_principal(request)
            _get_stored_conversation(fallback_principal, previous_id)  # expire stale histories
            if _response_store_key(fallback_principal, previous_id) not in _RESPONSE_STORE:
                return _error_response({'error': {
                    'type': 'invalid_request_error', 'code': 'previous_response_not_found',
                    'message': 'previous_response_id is unavailable to the Chat adapter. '
                               'Retry with the full conversation in input and omit previous_response_id.'}},
                    status_code=400)
        logger.info('[responses] native endpoint unavailable; using approved Chat adapter')

    # Translate Responses → Chat Completions (owner-approved adapter).
    principal = _request_principal(request)
    chat_body = responses_to_chat(body, principal=principal)
    is_stream = bool(chat_body.get("stream", False))

    # COMPATIBILITY_LAYER=2: Anthropic upstream — Responses → Chat → Anthropic
    # request; translate the Anthropic response back through OpenAI Chat to
    # Responses.
    if _is_anthropic_upstream():
        anthro_body = openai_chat_to_anthropic_request(chat_body)
        response = await _proxy_request("POST", "messages", anthro_body, path="messages",
                                        stream=is_stream, request=request, terminal_done=False)
        if isinstance(response, StreamingResponse):
            chat_sse = _translate_anthropic_stream_to_openai_chat(
                response.body_iterator, model, float(HEARTBEAT_MS) / 1000.0)
            return StreamingResponse(
                _remember_response_stream(
                    _translate_openai_chat_sse_to_responses(chat_sse, model),
                    principal, chat_body.get('messages', [])),
                on_close=response.aclose,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": response.headers.get("x-request-id", ""),
                **context_response_headers(response),
                },
            )
        if isinstance(response, JSONResponse):
            try:
                payload = json.loads(response.body)
            except Exception:
                return response
            if isinstance(payload, dict) and 'error' in payload:
                return response
            if isinstance(payload, dict) and payload.get("type") == "message" and "content" in payload:
                oai_chat = anthropic_to_openai_response(payload, model)
                resp_obj = chat_to_responses(model, oai_chat, body)
                _remember_completed_response(principal, resp_obj, chat_body.get('messages', []))
                return JSONResponse(resp_obj, status_code=response.status_code,
                                    headers=context_response_headers(response))
        return response

    response = await _proxy_request("POST", "chat/completions", chat_body,
                                    path="chat/completions", stream=is_stream, request=request)

    if isinstance(response, JSONResponse):
        # R-05: translate HERE — the raw OpenAI ChatCompletion body must never
        # reach Codex on the Responses surface.
        try:
            payload = json.loads(response.body)
        except Exception:
            return response
        if isinstance(payload, dict) and 'error' in payload:
            return JSONResponse(
                {"error": payload['error'], "type": "error"},
                status_code=response.status_code, headers=context_response_headers(response),
            )
        if isinstance(payload, dict) and 'choices' in payload:
            resp_obj = chat_to_responses(model, payload, body)
            _remember_completed_response(principal, resp_obj, chat_body.get('messages', []))
            return JSONResponse(resp_obj, status_code=response.status_code,
                                headers=context_response_headers(response))
        return response

    # Streaming: translate OpenAI SSE → Responses SSE event lifecycle using the
    # shared layer translator (§7), then persist the completed turn for
    # previous_response_id continuity (§2.2.7).
    if is_stream and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _remember_response_stream(
                _translate_openai_chat_sse_to_responses(response.body_iterator, model),
                principal, chat_body.get('messages', [])),
            on_close=response.aclose,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-request-id": response.headers.get("x-request-id", ""),
                **context_response_headers(response),
            },
        )

    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            resp = chat_to_responses(model, payload, body)
            _remember_completed_response(principal, resp, chat_body.get('messages', []))
            return JSONResponse(resp, status_code=response.status_code,
                                headers=context_response_headers(response))
    except Exception as e:
        logger.warning(f"[coarena] /v1/responses translation failed: {e}")
    return response


@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages API — native passthrough (upstream-native) or cross-dialect translation."""
    from common.context_recovery import context_response_headers
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # §4: unknown roles / orphan tool messages are rejected on the adapter.
    native_messages = (globals().get('NATIVE_MESSAGES_MODE', 'translate') != 'translate') or _is_anthropic_upstream()
    cross_dialect = (not native_messages) and not _is_anthropic_upstream()
    for _m in body.get("messages") or []:
        if cross_dialect and isinstance(_m, dict) and _m.get("role") not in (None, "user", "assistant", "tool", "system", "developer"):
            return _error_response({"error": {"message": f"Invalid role: {_m.get('role')!r}",
                                              "type": "invalid_request_error"}}, status_code=400)
        if isinstance(_m, dict) and _m.get("role") == "tool" and not _m.get("tool_use_id") and not _m.get("tool_call_id"):
            return _error_response({"error": {"message": "tool message requires tool_use_id",
                                              "type": "invalid_request_error"}}, status_code=400)

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    # Native upstream Messages is the production default. Translation is an
    # explicit compatibility choice (NATIVE_MESSAGES_MODE=translate), never an
    # accidental consequence of the global upstream dialect flag.
    if native_messages:
        _stream = bool(body.get("stream", False))
        return await _proxy_request("POST", "messages", body, path="messages",
                                    stream=_stream, request=request,
                                    native_stream=_stream, recover_context=True)

    # Translate Anthropic → OpenAI (cross-dialect adapter).
    openai_body = _anthropic_to_openai(body)
    openai_body["stream"] = body.get("stream", False)

    response = await _proxy_request("POST", "chat/completions", openai_body,
                                    path="chat/completions",
                                    stream=openai_body["stream"], request=request)

    if isinstance(response, JSONResponse):
        # R-05: translate HERE — a raw chat.completion body is unparseable for
        # Claude Code on the Messages surface.
        try:
            payload = json.loads(response.body)
        except Exception:
            return response
        if isinstance(payload, dict) and 'error' in payload:
            return JSONResponse(
                {"type": "error", "error": payload['error']},
                status_code=response.status_code, headers=context_response_headers(response),
            )
        if isinstance(payload, dict) and 'choices' in payload:
            return JSONResponse(_openai_to_anthropic_response(payload, body),
                                status_code=response.status_code,
                                headers=context_response_headers(response))
        return response

    # Streaming: translate OpenAI SSE → Anthropic SSE.
    if openai_body["stream"] and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_anthropic(response.body_iterator, model),
            on_close=response.aclose,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-request-id": response.headers.get("x-request-id", ""),
                **context_response_headers(response),
            },
        )

    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            anthropic_resp = _openai_to_anthropic_response(payload, body)
            return JSONResponse(anthropic_resp, status_code=response.status_code,
                                headers=context_response_headers(response))
    except Exception:
        pass
    return response


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Forward the native Coarena count-token semantics (upstream-native route)."""
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return await _proxy_request("POST", "messages/count_tokens", body,
                                path="messages/count_tokens",
                                stream=False, request=request, recover_context=False)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Shaped 501 — the Coarena upstream has no embeddings API (§2.1)."""
    try:
        await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return _error_response(
        {"error": {
            "message": "Coarena does not provide an embeddings API. "
                       "Use wrapper nvidia-python (port 9101) or openrouter (port 9103) "
                       "for embeddings.",
            "type": "not_implemented_error",
            "code": "embeddings_not_implemented"}},
        status_code=501,
    )


# ── Model discovery ──────────────────────────────────────────────────────

_MODELS_CACHE: dict = {'ts': 0.0, 'envelope': {}}
_MODELS_LOCK = asyncio.Lock()


async def _fetch_models_envelope() -> dict:
    """Fetch the upstream model list (no key required upstream)."""
    agent = await get_agent()
    async with agent.get(f"{COARENA_BASE}/models",
                         headers={"Accept": "application/json"}) as resp:
        if resp.status != 200:
            raise ValueError(f'model catalog unavailable (HTTP {resp.status})')
        data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}


async def _cached_models_envelope() -> dict:
    """TTL-cached native envelope; never invents rows."""
    now = time.time()
    if _MODELS_CACHE.get('envelope') and now - _MODELS_CACHE.get('ts', 0) < MODEL_CATALOG_TTL_SEC:
        return _MODELS_CACHE['envelope']
    async with _MODELS_LOCK:
        now = time.time()
        if _MODELS_CACHE.get('envelope') and now - _MODELS_CACHE.get('ts', 0) < MODEL_CATALOG_TTL_SEC:
            return _MODELS_CACHE['envelope']
        try:
            envelope = await _fetch_models_envelope()
            if isinstance(envelope.get('data'), list) and envelope.get('data'):
                _MODELS_CACHE['ts'] = now
                _MODELS_CACHE['envelope'] = envelope
                ids = [str(m.get('id')) for m in envelope['data'] if isinstance(m, dict) and m.get('id')]
                try:
                    MODEL_REGISTRY.register_catalog(envelope['data'])
                except Exception as exc:
                    logger.debug(f'[coarena] catalog registration skipped: {exc}')
                return envelope
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'[coarena] model catalog refresh failed: {e}')
    return _MODELS_CACHE.get('envelope') or {}


@app.get("/v1/models")
async def list_models(request: Request):
    """Native Coarena model list (native envelope rows + local context metadata).

    Final audit 2026-11-15: every active wrapper enriches catalog rows with
    the shared ``discovery_limits`` normalization (context_window,
    max_output_tokens, context_metadata status) while preserving native
    fields verbatim; coarena previously returned the raw envelope and made
    fleet clients blind to context limits. Native rows are kept unchanged
    apart from the additive local metadata.
    """
    from common.model.metadata import discovery_limits
    try:
        envelope = await _cached_models_envelope()
        if envelope:
            rows = envelope.get('data')
            if isinstance(rows, list):
                enriched = []
                for row in rows:
                    if isinstance(row, dict):
                        enriched.append({**row, **discovery_limits(row)})
                    else:
                        enriched.append(row)
                return JSONResponse({**envelope, 'data': enriched})
            return JSONResponse(envelope)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f'[coarena] /v1/models cache miss: {e}')
    # Live native passthrough on cache failure.
    return await _proxy_request("GET", "models", None, path="models",
                                request=request, recover_context=False)


@app.get("/v1/models/{model_id:path}")
async def get_model_detail(model_id: str, request: Request):
    """Native passthrough for a single upstream model."""
    return await _proxy_request("GET", f"models/{model_id}", None,
                                path=f"models/{model_id}",
                                request=request, recover_context=False)


@app.get("/api/tags")
async def api_tags():
    """Ollama-compatible model discovery derived from the upstream list."""
    envelope = await _cached_models_envelope()
    out_models = []
    for m in (envelope.get('data') or []):
        if not isinstance(m, dict):
            continue
        mid = m.get('id', '')
        if not mid:
            continue
        family = mid.split('-')[0] if '-' in mid else mid
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


@app.get("/v1/capabilities")
async def capabilities(request: Request):
    """Model capability discovery (contract §2.1).

    Coarena's catalog publishes plain model ids without per-model parameter
    metadata, so capability_state is honestly 'unknown' until runtime
    observations fill the shared registry.
    """
    envelope = await _cached_models_envelope()
    models_list = envelope.get('data') or []
    capability_models = []
    for m in models_list:
        item = m if isinstance(m, dict) else {}
        mid = item.get('id') if item else m
        capability_models.append({
            'id': mid,
            'capabilities': ['chat', 'tools', 'reasoning'],
            'capability_source': 'provider_catalog',
            'capability_state': 'known',
            'streaming': True,
            'endpoints': ['chat_completions', 'responses', 'messages'],
            'wrapper_endpoints': ['/v1/chat/completions', '/v1/responses', '/v1/messages',
                                  '/v1/messages/count_tokens'],
            'routing_metadata': {},
            'input_modalities': ['text'],
            'output_modalities': ['text'],
        })
    return {
        'object': 'list',
        'models': capability_models,
        'summary': {'total': len(capability_models)},
        'catalog_state': 'provider_poll_cache',
        'dynamic_alias_target': None,
    }


# ── Coarena provider extras (upstream-native, authenticated) ─────────────

@app.get("/v1/limits")
async def limits(request: Request):
    """Today's use, the account's limits and any open ask (upstream-native)."""
    return await _proxy_request("GET", "limits", None, path="limits",
                                request=request, recover_context=False)


@app.get("/v1/usage")
async def usage(request: Request):
    """Aggregated usage for the calling key (upstream-native)."""
    return await _proxy_request("GET", "usage", None, path="usage",
                                request=request, recover_context=False)


@app.post("/v1/feedback")
async def feedback(request: Request):
    """Rate a reply by its id (upstream-native)."""
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return await _proxy_request("POST", "feedback", body, path="feedback",
                                request=request, recover_context=False)


# ── Responses API ↔ Chat Completions translation (owner-approved adapter) ─

# Tenant-isolated response store for previous_response_id continuity.
# B-33 fix: bounded on all three axes — entry count, total bytes, TTL.
_RESPONSE_STORE: "OrderedDict[str, tuple]" = OrderedDict()
_RESPONSE_STORE_MAX_ENTRIES = int(os.environ.get('RESPONSES_STORE_MAX_ENTRIES', '200'))
_RESPONSE_STORE_TTL_SEC = int(os.environ.get('RESPONSES_STORE_TTL_SEC', '3600'))
_RESPONSE_STORE_MAX_BYTES = int(os.environ.get('RESPONSES_STORE_MAX_BYTES', str(32 * 1024 * 1024)))


def _store_response(principal: str, response_id: str, messages: list) -> None:
    """Bounded, TTL-pruned write to the response store (B-33)."""
    if not response_id or not principal:
        return
    key = _response_store_key(principal, response_id)
    try:
        serialized = json.dumps(messages, ensure_ascii=False)
        size = len(serialized.encode('utf-8'))
    except Exception:
        return
    now = time.time()
    _RESPONSE_STORE[key] = (now, size, messages)
    _prune_response_store()


def _prune_response_store() -> None:
    """Three-axis eviction: entry count, total bytes, TTL."""
    now = time.time()
    for key in list(_RESPONSE_STORE.keys()):
        entry = _RESPONSE_STORE.get(key)
        if not entry:
            continue
        ts = entry[0] if isinstance(entry, tuple) else 0.0
        if now - ts > _RESPONSE_STORE_TTL_SEC:
            _RESPONSE_STORE.pop(key, None)
    # Entry-count bound: evict the OLDEST entries first (insertion order).
    while len(_RESPONSE_STORE) > _RESPONSE_STORE_MAX_ENTRIES:
        oldest = next(iter(_RESPONSE_STORE), None)
        if oldest is None:
            break
        _RESPONSE_STORE.pop(oldest, None)
    # Total-bytes bound: drop oldest until under the byte ceiling.
    total = 0
    sizes = {}
    for key, entry in _RESPONSE_STORE.items():
        size = entry[1] if isinstance(entry, tuple) else 0
        sizes[key] = size
        total += size
    for key in list(_RESPONSE_STORE.keys()):
        if total <= _RESPONSE_STORE_MAX_BYTES:
            break
        total -= sizes.get(key, 0)
        _RESPONSE_STORE.pop(key, None)


def _get_stored_conversation(principal: str, response_id: str) -> list:
    """TTL-aware read of a stored conversation (bounded store, §6.3)."""
    key = _response_store_key(principal, response_id)
    entry = _RESPONSE_STORE.get(key)
    if not entry:
        return []
    ts, _size, messages = entry
    if time.time() - ts > _RESPONSE_STORE_TTL_SEC:
        _RESPONSE_STORE.pop(key, None)
        return []
    return copy.deepcopy(messages)


def _response_store_key(principal: str, response_id: str) -> str:
    import hashlib
    return hashlib.sha256(f"{principal}|{response_id}".encode()).hexdigest()


def _request_principal(request: Request) -> str:
    """Stable per-client principal for response-store namespacing (§6.3)."""
    tok = authenticated_client_token(getattr(request, 'headers', {}) or {})
    if not tok:
        return 'anon'
    import hashlib
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def responses_to_chat(body: dict, principal: str = '') -> dict:
    """Convert OpenAI Responses API request → Chat Completions request.

    Handles `input` as string or array, `instructions`, tenant-scoped
    `previous_response_id` continuity, function_call/function_call_output
    items, and forwards all client params verbatim (transparent proxy).
    """
    from copy import deepcopy
    body = deepcopy(body)
    model = body.get('model') or ''
    msgs: list = []
    prev = body.get('previous_response_id')
    if prev and principal:
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
            if t == 'reasoning':
                continue  # chat has no reasoning placeholder
            if t == 'function_call_output':
                outv = it.get('output', '')
                msgs.append({'role': 'tool', 'tool_call_id': it.get('call_id') or '',
                             'content': outv if isinstance(outv, str) else json.dumps(outv, ensure_ascii=False)})
            elif t == 'function_call':
                args = it.get('arguments', '')
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                msgs.append({'role': 'assistant', 'content': None,
                             'tool_calls': [{'id': it.get('call_id') or it.get('id') or 'call_1',
                                             'type': 'function',
                                             'function': {'name': it.get('name', '') or '',
                                                          'arguments': args}}]})
            else:
                role = it.get('role', 'user')
                if role == 'developer':
                    role = 'system'
                c = it.get('content', '')
                if isinstance(c, list):
                    c = ''.join(p.get('text', '') for p in c
                                if isinstance(p, dict) and p.get('type') in ('input_text', 'text', 'output_text'))
                msgs.append({'role': role or 'user', 'content': c})
    if body.get('instructions'):
        if msgs and msgs[0].get('role') == 'system':
            msgs[0]['content'] = body['instructions'] + '\n\n' + str(msgs[0].get('content') or '')
        else:
            msgs.insert(0, {'role': 'system', 'content': body['instructions']})
    out = {'model': model, 'messages': msgs, 'stream': bool(body.get('stream', False))}
    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = body['max_output_tokens']
    elif body.get('max_tokens') is not None:
        out['max_tokens'] = body['max_tokens']
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
            tools.append({'type': 'function', 'function': {
                'name': name,
                'description': fn.get('description', '') or '',
                'parameters': fn.get('parameters') or fn.get('input_schema') or {},
                **({'strict': fn['strict']} if 'strict' in fn else {}),
            }})
        if tools:
            out['tools'] = tools
    choice = out.get('tool_choice')
    if isinstance(choice, dict) and choice.get('type') == 'function' and isinstance(choice.get('name'), str):
        out['tool_choice'] = {'type': 'function', 'function': {'name': choice['name']}}
    return out


def chat_to_responses(model: str, data: dict, request_body: dict | None = None) -> dict:
    """Convert OpenAI Chat Completions response → Responses API response."""
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    output = []
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    if reasoning:
        output.append({'id': f"rsn_{int(time.time()*1000)}", 'type': 'reasoning',
                       'status': 'completed', 'summary': [],
                       'content': [{'type': 'reasoning_text', 'text': reasoning}]})
    for tc in msg.get('tool_calls') or []:
        fn = tc.get('function') or {}
        output.append({'id': tc.get('id') or f'fc_{len(output)}', 'type': 'function_call',
                       'status': 'completed', 'call_id': tc.get('id'),
                       'name': fn.get('name', '') or '',
                       'arguments': fn.get('arguments', '') or ''})
    output.append({'id': f"msg_{int(time.time()*1000)}", 'type': 'message',
                   'status': 'completed', 'role': 'assistant',
                   'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
    u = data.get('usage') or {}
    # The openai SDK's Response model REQUIRES top-level parallel_tool_calls /
    # tool_choice / tools — missing them fails client.responses.create().
    resp = {'id': data.get('id') or f"resp_{int(time.time()*1000)}",
            'object': 'response', 'created_at': int(time.time()),
            'model': model, 'status': 'completed', 'output': output,
            'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [],
            'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0,
                      'output_tokens': u.get('completion_tokens', 0) or 0,
                      'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}}
    return resp


def _remember_completed_response(principal: str, response: dict, messages: list):
    """Save the assistant turn as well as its input for the next tool result."""
    if not principal or not response.get('id') or response.get('status') != 'completed':
        return
    text_parts = []
    reasoning_parts = []
    tool_calls = []
    for item in response.get('output') or []:
        if item.get('type') == 'message':
            text_parts.extend(part.get('text', '') for part in item.get('content') or []
                              if part.get('type') == 'output_text')
        elif item.get('type') == 'reasoning':
            reasoning_parts.extend(part.get('text', '') for part in item.get('content') or []
                                   if part.get('type') == 'reasoning_text')
        elif item.get('type') == 'function_call':
            tool_calls.append({'id': item.get('call_id') or item.get('id'), 'type': 'function',
                               'function': {'name': item.get('name', ''),
                                            'arguments': item.get('arguments', '')}})
    assistant = {'role': 'assistant', 'content': ''.join(text_parts) or (None if tool_calls else '')}
    if tool_calls:
        assistant['tool_calls'] = tool_calls
    if reasoning_parts:
        assistant['reasoning_content'] = ''.join(reasoning_parts)
    _store_response(principal, response['id'], list(messages) + [assistant])


async def _remember_response_stream(generator, principal: str, messages: list):
    """Persist a successful turn before exposing its terminal event."""
    try:
        async for frame in generator:
            text = frame.decode('utf-8') if isinstance(frame, bytes) else frame
            for line in text.splitlines():
                if not line.startswith('data:'):
                    continue
                payload = line[5:].strip()
                if not payload or payload == '[DONE]':
                    continue
                try:
                    event = json.loads(payload)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict) and event.get('type') == 'response.completed':
                    _remember_completed_response(principal, event.get('response') or {}, messages)
            yield frame
    finally:
        close = getattr(generator, 'aclose', None)
        if close is not None:
            await close()


# ── Cross-dialect converters (§2.2.8/§3.6: transparent, non-mutating) ─────

def _anthropic_to_openai(body: dict) -> dict:
    """Convert Anthropic Messages API request to OpenAI Chat Completions.

    Correctly handles: system field as top-level system message, tool_result
    blocks → tool role messages, tool_use blocks → tool_calls, image blocks →
    image_url, thinking blocks → reasoning_content. Never mutates the caller's
    body; the outbound converted schema is owned by this copy.
    """
    from copy import deepcopy
    body = deepcopy(body)
    messages = []
    system = body.get("system", "")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            sys_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict) and b.get("type") == "text")
            if sys_text:
                messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content")

        if role == "assistant":
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                reasoning_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype in ("thinking", "reasoning"):
                        reasoning_parts.append(block.get("thinking") or block.get("reasoning") or "")
                    elif btype == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                msg_obj = {"role": "assistant"}
                if text_parts:
                    msg_obj["content"] = "\n".join(text_parts)
                else:
                    msg_obj["content"] = None
                if tool_calls:
                    msg_obj["tool_calls"] = tool_calls
                if reasoning_parts:
                    msg_obj["reasoning_content"] = "\n".join(reasoning_parts)
                messages.append(msg_obj)
            else:
                messages.append({"role": "assistant", "content": content})
        elif role == "user":
            if isinstance(content, list):
                text_content = []
                tool_results = []
                image_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_content.append(block.get("text", ""))
                    elif btype == "tool_result":
                        tc_content = block.get("content", "")
                        if isinstance(tc_content, list):
                            tc_text_parts = []
                            for tc_block in tc_content:
                                if isinstance(tc_block, dict) and tc_block.get("type") == "text":
                                    tc_text_parts.append(tc_block.get("text", ""))
                            tc_content = "\n".join(tc_text_parts)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block.get("tool_use_id", ""),
                            "content": tc_content if isinstance(tc_content, str) else json.dumps(tc_content),
                        })
                    elif btype == "image":
                        src = block.get("source", {})
                        if src.get("type") == "base64":
                            media_type = src.get("media_type", "image/png")
                            data = src.get("data", "")
                            image_parts.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:{media_type};base64,{data}"},
                            })
                        else:
                            url = src.get("url", "")
                            if url:
                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": url},
                                })
                # tool_result messages precede the user text message (OpenAI
                # requires tool results to follow the assistant tool_calls).
                for tr in tool_results:
                    messages.append(tr)
                if text_content:
                    user_content = "\n".join(text_content) if not image_parts else [
                        {"type": "text", "text": "\n".join(text_content)}
                    ] + image_parts
                    messages.append({"role": "user", "content": user_content})
                elif image_parts:
                    messages.append({"role": "user", "content": image_parts})
            else:
                messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": role, "content": content})

    model = body.get("model", "")
    # TRANSPARENT PROXY: only set max_tokens if the client explicitly sent one.
    openai_body = {
        "model": model,
        "messages": messages,
    }
    if body.get("max_tokens") is not None:
        openai_body["max_tokens"] = body["max_tokens"]
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
            openai_body[dst] = body[src]

    explicit_reasoning = body.get("reasoning")
    reasoning = dict(explicit_reasoning) if isinstance(explicit_reasoning, dict) else {}
    thinking = body.get("thinking")
    thinking_type = thinking.get("type") if isinstance(thinking, dict) else None
    output_config = body.get("output_config")
    effort = output_config.get("effort") if isinstance(output_config, dict) else None
    mapped_reasoning = None
    if thinking_type == "disabled":
        mapped_reasoning = {"enabled": False}
    elif effort is not None:
        mapped_reasoning = {"effort": effort}
    elif thinking_type == "adaptive":
        mapped_reasoning = {"enabled": True}
    elif thinking_type == "enabled":
        if thinking.get("budget_tokens") is not None:
            mapped_reasoning = {"max_tokens": thinking["budget_tokens"]}
        else:
            mapped_reasoning = {"enabled": True}
    if explicit_reasoning is not None and not isinstance(explicit_reasoning, dict):
        openai_body["reasoning"] = explicit_reasoning
    elif mapped_reasoning is not None:
        for key in ("enabled", "effort", "max_tokens"):
            reasoning.pop(key, None)
        openai_body["reasoning"] = {**reasoning, **mapped_reasoning}
    elif isinstance(explicit_reasoning, dict):
        openai_body["reasoning"] = reasoning

    _strip_cache(openai_body)
    if body.get("tools"):
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}) or {"type": "object", "properties": {}},
                    **({"strict": t["strict"]} if "strict" in t else {}),
                },
            }
            for t in body["tools"] if isinstance(t, dict) and t.get("name")
        ]
    if body.get("tool_choice"):
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            if tc.get("type") == "any":
                openai_body["tool_choice"] = "required"
            elif tc.get("type") in ("auto", "none"):
                openai_body["tool_choice"] = tc["type"]
            elif tc.get("type") == "tool":
                openai_body["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        elif isinstance(tc, str):
            openai_body["tool_choice"] = tc
    return openai_body


def _openai_to_anthropic_response(openai_resp: dict, request_body: dict) -> dict:
    """Convert OpenAI ChatCompletion response → Anthropic Message response.

    Self-contained by contract for isolated AST sandboxes: reasoning_content
    surfaces as a thinking block (transparency parity), stop_reason maps
    strictly from finish_reason, and the wrapper owns the Anthropic message id
    prefix (C-06 parity — a bare upstream OpenAI id never becomes msg id).
    """
    if isinstance(openai_resp, dict) and openai_resp.get("type") == "message" and "content" in openai_resp:
        return openai_resp
    choices = openai_resp.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {}) or {}
    content_blocks = []
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    if reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": _thinking_signature()})
    _or_text = message.get("content")
    if isinstance(_or_text, str):
        if not reasoning:
            _inline, _or_text = _extract_inline_thinking(_or_text)
            if _inline:
                reasoning = _inline
                content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": _thinking_signature()})
        else:
            _or_text = _strip_think_tags(_or_text)
    if _or_text:
        content_blocks.append({"type": "text", "text": _or_text})
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            fn = tc.get("function", {})
            try:
                input_obj = json.loads(fn.get("arguments", "{}"))
            except Exception:
                input_obj = {"raw": fn.get("arguments", "")}
            content_blocks.append({
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "input": input_obj,
            })
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                   "function_call": "tool_use", "content_filter": "refusal"}.get(finish_reason, "end_turn")

    usage = openai_resp.get("usage", {})
    res_id = openai_resp.get("id") or f"msg_{int(time.time() * 1000)}"
    if not (isinstance(res_id, str) and res_id.startswith("msg_")):
        res_id = f"msg_{res_id}"
    return {
        "id": res_id,
        "type": "message",
        "role": "assistant",
        "model": (request_body or {}).get("model", ""),
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _translate_openai_stream_to_anthropic(openai_gen, model: str):
    """Translate OpenAI Chat SSE → Anthropic Messages SSE using the shared
    AnthropicStreamState (§7): full lifecycle, parallel tool blocks kept open
    concurrently (R-02), strict finish→stop mapping (B-06), error frames
    surfaced terminally (R-03), post-finish drops counted (B-05).

    Terminates with message_delta + message_stop on success; a surfaced error
    event is terminal on its own — never a fabricated end_turn.
    """
    state = AnthropicStreamState(model=model)
    buffer = ''
    try:
        async for chunk in openai_gen:
            if isinstance(chunk, (bytes, bytearray)):
                text = bytes(chunk).decode('utf-8', 'replace')
            elif isinstance(chunk, str):
                text = chunk
            else:
                continue
            if not state.message_started:
                for ev in state.start_events():
                    yield ev
            buffer += text.replace('\r\n', '\n')
            while '\n\n' in buffer:
                frame, buffer = buffer.split('\n\n', 1)
                for line in frame.split('\n'):
                    line = line.strip()
                    if not line.startswith('data:'):
                        continue  # comments/id/retry lines are tolerated
                    data_str = line[5:].strip()
                    if not data_str or data_str == '[DONE]':
                        continue  # bare data: is a keep-alive, not EOF (B-01)
                    try:
                        payload = json.loads(data_str)
                    except (ValueError, TypeError):
                        # B-10: unparsable frames are logged and dropped,
                        # never synthesised into assistant content.
                        logger.warning('[coarena] dropping unparsable upstream SSE frame')
                        continue
                    if not isinstance(payload, dict):
                        continue
                    for ev in state.translate_chunk(payload):
                        yield ev
        for ev in state.force_done():
            yield ev
    except (GeneratorExit, asyncio.CancelledError):
        # B-09: never yield during finalization — re-raise, cleanup in finally.
        raise
    except Exception as e:
        logger.error(f'[coarena] anthropic stream translation error: {e}')
        for ev in state.force_done():
            yield ev
    finally:
        try:
            aclose = getattr(openai_gen, 'aclose', None)
            if aclose is not None:
                await aclose()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# OBSERVABILITY ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "git_dirty": GIT_DIRTY,
        "keys": pool.available_keys,
        "available": pool.available_keys,
        "total_keys": pool.total_keys,
        "in_flight": sum(k.in_flight for k in pool.keys),
        "uptime_seconds": int(time.time() - metrics.start),
        "compatibility_layer": (os.environ.get('COMPATIBILITY_LAYER') or '1').strip() or '1',
        "free_only": free_only_enabled(),
    }


@app.get("/ready")
async def ready():
    return {
        "ready": pool.total_keys > 0,
        "keys_loaded": pool.total_keys,
        "free_only": free_only_enabled(),
    }


@app.get("/metrics")
async def metrics_json():
    return {
        "provider": "coarena",
        "total_requests": metrics.requests,
        "total_tokens": metrics.tokens_in + metrics.tokens_out,
        "input_tokens": metrics.tokens_in,
        "output_tokens": metrics.tokens_out,
        "errors": metrics.errors,
        "uptime_seconds": int(time.time() - metrics.start),
        "key_pool": pool.health_json(),
    }


@app.get("/metrics/prom")
async def prom_metrics():
    return Response(
        content=pool.prom_metrics() + "\n" + metrics.prom_metrics(),
        media_type="text/plain; version=0.0.4",
    )


@app.get("/metrics/model-status")
async def model_status(request: Request):
    return {
        "provider": "coarena",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": MODEL_STORE.status_map(),
    }


@app.get("/metrics/models")
async def metrics_models():
    return {"models": metrics.per_model_summary(), "provider": "coarena"}


@app.get("/metrics/keys")
async def metrics_keys(request: Request):
    window = request.query_params.get('window', '24h')
    live = {}
    try:
        for k in pool.all_stats():
            if isinstance(k, dict) and k.get("label"):
                live[k["label"]] = k
    except Exception:
        live = {}
    rows = metrics.per_key_summary()
    seen = set()
    for row in rows:
        row["live"] = live.get(row["key_label"], {})
        seen.add(row["key_label"])
    for label, lv in live.items():
        if label not in seen:
            rows.append({
                "key_label": label, "requests": 0, "rate_limited_count": 0,
                "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "last_used_ago_s": None, "live": lv,
            })
    return {"window": window, "keys": rows, "provider": "coarena"}


@app.get("/metrics/activity")
async def metrics_activity(request: Request):
    try:
        limit = max(1, min(500, int(request.query_params.get('limit', '50'))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.query_params.get('offset', '0')))
    except (TypeError, ValueError):
        offset = 0
    rows = metrics.recent_activity(limit=limit, offset=offset)
    return {"limit": limit, "offset": offset, "count": len(rows), "rows": rows, "provider": "coarena"}


@app.get("/metrics/chart/hourly")
async def metrics_chart_hourly(request: Request):
    try:
        hours = max(1, min(168, int(request.query_params.get('hours', '24'))))
    except (TypeError, ValueError):
        hours = 24
    return {"hours": hours, "data": metrics.hourly_chart(hours), "provider": "coarena"}


@app.get("/stats")
async def stats():
    s = await metrics.summary()
    s["key_pool"] = pool.health_json()
    s["free_only"] = free_only_enabled()
    return s


@app.get("/dashboard")
async def dashboard():
    return _error_response({
        "error": "Per-wrapper dashboards are disabled; use unified dashboard on port 9106"
    }, status_code=410)


@app.get("/version")
async def version():
    return {
        "app": "wrapper-coarena",
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "git_dirty": GIT_DIRTY,
        "upstream": "Coarena",
        "source_root": SOURCE_ROOT,
    }


# ══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def main():
    """Entry point for `python -m coarena.src.main`."""
    import uvicorn
    validate_config()
    uvicorn.run(
        "src.main:app",
        host=BIND_HOST,
        port=LISTEN_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
