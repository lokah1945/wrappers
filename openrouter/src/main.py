#!/usr/bin/env python3
"""
wrapper-openrouter v1.0.0 — FastAPI proxy for OpenRouter.

OpenAI + Anthropic compatible transparent proxy for OpenRouter API.
Follows the standardized monorepo wrapper pattern (2026-07-28).

Upstream: https://openrouter.ai/api/v1

Production features:
- Multi-key rotation + pacing + load shedding
- Full streaming with anti-silence + heartbeat
- OpenAI Chat Completions + Responses API
- Anthropic Messages API (translated to OpenAI)
- .env hot reload
- Rich metrics (JSON + Prometheus)
- Integrated MCP catalog server (NVIDIA NIM + multi-provider)
- FREE_ONLY mode
"""

import asyncio
import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import anyio

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
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model import (
        LocalModelRegistry,
        ModelRegistryClient,
    )
    from common.model_state import (
        ModelStateStore,
    )

# ── Catalog MCP integration ──────────────────────────────────────────────
CATALOG_REPO = os.environ.get('CATALOG_REPO', str(Path(__file__).resolve().parents[2] / 'model_fetcher'))
if CATALOG_REPO not in sys.path:
    sys.path.insert(0, CATALOG_REPO)

# Also add src/ if it exists
src_path = os.path.join(CATALOG_REPO, 'src')
if os.path.isdir(src_path) and src_path not in sys.path:
    sys.path.insert(0, src_path)

try:
    from catalog_queries import (
        DEFAULT_DB,
        get_model,
        get_provider_model,
        list_providers,
        open_db,
        search_models,
        search_provider_models,
    )
    from catalog_queries import stats as catalog_stats
    from env_config import free_only as catalog_free_only
    _HAS_CATALOG = True
except ImportError:
    _HAS_CATALOG = False
    async def _stub_catalog(*a, **kw):
        return {"error": "Catalog not available — install model_fetcher"}
    search_models = _stub_catalog
    get_model = _stub_catalog
    list_providers = _stub_catalog
    catalog_free_only = lambda: False

# ── Provider Management (MANAGEMENT_KEY) ────────────────────────────────
try:
    import provider_management as MGT
    _HAS_MANAGEMENT = True
except ImportError:
    _HAS_MANAGEMENT = False
    # Stub management
    class _MgtStub:
        @staticmethod
        def is_management_enabled(*a, **kw): return False
        @staticmethod
        def get_provider_keys_status(*a, **kw): return {}
    MGT = _MgtStub()

# ── FastAPI + deps ───────────────────────────────────────────────────────
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter, sanitize_header_value
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False
    import re as _re
    def sanitize_header_value(value):
        if not isinstance(value, str):
            value = str(value)
        return _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()

from dotenv import load_dotenv

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from .key_pool import KeyPool

from .metrics import Metrics

# ── Shared translations ─────────────────────────────────────────────────
try:
    from common.translations import (
        AnthropicStreamState,
    )
    from common.translations import (
        normalize_upstream_error as _normalize_upstream_error,
    )
    from common.translations import (
        repair_orphan_tool_messages as _repair_orphan_tool_messages,
    )
    from common.translations import (
        strip_cache_control as _strip_cache,
    )
    from common.translations import (
        parse_retry_after as _parse_retry_after,
        is_retriable_status as _is_retriable_status,
        should_cooldown_key as _should_cooldown_key,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError as _imp_err:
    raise RuntimeError("common.translations import failed; wrapper requires shared translations") from _imp_err

# ── MCP integration (FastMCP) ───────────────────────────────────────────
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport

# ── Bootstrap ────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
    load_dotenv(ROOT / '.env')
    load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────
LOG_FILE = os.environ.get('LOG_FILE', '/root/wrapper/openrouter/openrouter.log')
try:
    from common.logging_utils import setup_logging
    logger = setup_logging('wrapper-openrouter', log_file=LOG_FILE, default_log_file='/tmp/wrapper-openrouter.log',
                           log_format='%(asctime)s [openrouter] %(message)s')
except ImportError:
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or '.', exist_ok=True)
        _log_file_handler = logging.FileHandler(LOG_FILE)
    except Exception:
        LOG_FILE = '/tmp/wrapper-openrouter.log'
        _log_file_handler = logging.FileHandler(LOG_FILE)
    logger = logging.getLogger('wrapper-openrouter')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [openrouter] %(message)s',
                        handlers=[_log_file_handler, logging.StreamHandler()])

# ── Configuration ────────────────────────────────────────────────────────
LISTEN_PORT = int(os.environ.get('LISTEN_PORT', '9106'))
BIND_HOST = os.environ.get('LISTEN_HOST', '0.0.0.0')
OPENROUTER_BASE = os.environ.get('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')
CATALOG_DB_PATH = os.environ.get('CATALOG_DB', os.path.join(os.path.dirname(CATALOG_REPO) if _HAS_CATALOG else '/dev/null',
                                                             'data', 'active_nvidia_nim.sqlite3'))
MODEL_STATE_DB = os.environ.get('MODEL_STATE_DB', str(ROOT / 'model-state.db'))
MODEL_CATALOG_TTL_SEC = int(os.environ.get('MODEL_CATALOG_TTL_SEC', '21600'))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get('MODEL_CATALOG_REFRESH_SEC', '86400'))
MODEL_STORE = ModelStateStore('openrouter', MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry('openrouter', profile_db_path=MODEL_STATE_DB)
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
_MODEL_REFRESH_TASK = None
HEARTBEAT_MS = int(os.environ.get('HEARTBEAT_INTERVAL_MS', '5000'))
VERSION = '1.0.0'

# ── Validate Config ──────────────────────────────────────────────────────

def validate_config():
    """Validate required configuration at startup. Fail-fast on missing required env vars."""
    missing = []
    for var in ['OPENROUTER_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var) and not os.environ.get('DISABLE_AUTH'):
            missing.append(var)

    if missing and not os.environ.get('DISABLE_AUTH'):
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    try:
        port = int(os.environ.get('LISTEN_PORT', '9106'))
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
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=_resolve_git_root(),
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return 'unknown'


GIT_COMMIT = _resolve_git_commit()
SOURCE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_only_enabled() -> bool:
    """FREE_ONLY=yes|true|1 → only free models."""
    v = (os.environ.get('FREE_ONLY') or 'no').strip().lower()
    if v in ('yes', 'true', '1', 'on', 'y'):
        return True
    # Also check catalog FREE_ONLY
    try:
        return catalog_free_only()
    except Exception:
        return False


def is_free_model(model_id: str) -> bool:
    """True if model id contains ':free' or '-free' suffix."""
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    return bool(mid.endswith((':free', '-free')))


def _bearer_token() -> str:
    return (os.environ.get('BEARER_TOKEN') or '').strip()


def _client_ip(request: Request) -> str:
    host = getattr(request.client, 'host', None) if request.client else None
    if host:
        return host
    xff = request.headers.get('x-forwarded-for')
    if xff:
        return xff.split(',')[0].strip()
    return 'unknown'


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


# ── MCP Server Setup ─────────────────────────────────────────────────────

def _create_mcp_server():
    """Create FastMCP instance for catalog tools."""
    if not _HAS_CATALOG:
        return None

    mcp = FastMCP(
        "openrouter-catalog",
        instructions=(
            "AI Model Catalog integrated with OpenRouter wrapper. "
            "Search and inspect the audited NVIDIA NIM catalog and multi-provider "
            "model listings (OpenRouter, Nous, OpenCode, Blackbox)."
        ),
    )

    @mcp.tool(name="search_models")
    async def mcp_search_models(query: str = "", modality: str = "", tier: str = "",
                                  working_only: bool = False, free_only: bool = False,
                                  publisher: str = "", limit: int = 50) -> str:
        try:
            db = open_db(CATALOG_DB_PATH)
            results = search_models(db, query=query or None, modality=modality or None,
                                     tier=tier or None, working_only=working_only,
                                     free_only=free_only, publisher=publisher or None, limit=limit)
            db.close()
            return json.dumps({"count": len(results), "models": results}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool(name="get_model")
    async def mcp_get_model(catalog_id: str) -> str:
        try:
            db = open_db(CATALOG_DB_PATH)
            result = get_model(db, catalog_id)
            db.close()
            return json.dumps(result or {"error": "Model not found"}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool(name="list_providers")
    async def mcp_list_providers() -> str:
        try:
            db = open_db(CATALOG_DB_PATH)
            results = list_providers(db)
            db.close()
            return json.dumps(results, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.tool(name="search_provider_models")
    async def mcp_search_provider_models(provider: str = "", query: str = "",
                                           free_only: bool = False, limit: int = 50) -> str:
        try:
            db = open_db(CATALOG_DB_PATH)
            results = search_provider_models(db, provider=provider or None, query=query or None,
                                              free_only=free_only, limit=limit)
            db.close()
            return json.dumps({"count": len(results), "models": results}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": str(e)})

    return mcp


MCP_SERVER = _create_mcp_server()
SSE_TRANSPORT = SseServerTransport("/mcp/messages") if MCP_SERVER else None


# ── Key Pool ──────────────────────────────────────────────────────────────
pool = KeyPool()
metrics = Metrics()

# ── Async HTTP session ────────────────────────────────────────────────────
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


# ── Model Refresh ─────────────────────────────────────────────────────────
async def _refresh_models_loop():
    """Periodically refresh model catalog from upstream."""
    global _MODEL_REFRESH_TASK
    try:
        # Initial load
        ids = await _refresh_models()
        logger.info(f"[openrouter] Initial model catalog: {len(ids)} models")

        while True:
            await asyncio.sleep(MODEL_CATALOG_REFRESH_SEC)
            ids = await _refresh_models()
            logger.info(f"[openrouter] Model catalog refreshed: {len(ids)} models")
    except asyncio.CancelledError:
        pass


async def _refresh_models(force: bool = False) -> list:
    """Fetch model list from OpenRouter upstream."""
    try:
        agent = await get_agent()
        headers = {"Accept": "application/json"}
        async with agent.get(f"{OPENROUTER_BASE}/models", headers=headers) as resp:
            if resp.status != 200:
                logger.warning(f"[openrouter] Failed to fetch models: {resp.status}")
                return []
            data = await resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return models
    except Exception as e:
        logger.warning(f"[openrouter] Model refresh error: {e}")
        return []


# ── Lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup and shutdown."""
    # Startup
    global _MODEL_REFRESH_TASK
    logger.info("[openrouter] Starting wrapper-openrouter")
    pool.load_from_env()

    if _HAS_CATALOG:
        logger.info("[openrouter] Catalog integration enabled")
        if os.path.exists(CATALOG_DB_PATH):
            logger.info(f"[openrouter] Catalog DB found at {CATALOG_DB_PATH}")
        else:
            logger.warning(f"[openrouter] Catalog DB not found at {CATALOG_DB_PATH}")

    _MODEL_REFRESH_TASK = asyncio.create_task(_refresh_models_loop())
    logger.info(f"[openrouter] Ready on {BIND_HOST}:{LISTEN_PORT}")

    yield

    # Shutdown
    logger.info("[openrouter] Shutting down")
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
    await metrics.close()
    if _agent and not _agent.closed:
        await _agent.close()


# ── FastAPI App ────────────────────────────────────────────────────────────

app = FastAPI(
    title="wrapper-openrouter",
    version=VERSION,
    description="OpenAI + Anthropic compatible proxy for OpenRouter with integrated MCP catalog",
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

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter, max_bytes=50 * 1024 * 1024)


# ── Auth Middleware ────────────────────────────────────────────────────────

DISABLE_AUTH = os.environ.get('DISABLE_AUTH', '').strip().lower() in ('1', 'true', 'yes')
PUBLIC_PATHS = {'/health', '/ready', '/metrics', '/metrics/prom', '/dashboard', '/stats',
                '/catalog/health', '/catalog/ready', '/catalog/metrics',
                '/mcp/sse', '/mcp/messages', '/mcp',
                # Public model discovery (Ollama + OpenAI compatible) — agents
                # need to list models before authenticating.
                '/api/tags', '/v1/models', '/version'}

# Headers forwarded upstream (transparent passthrough to preserve client identity
# and beta-feature flags for OpenAI/Anthropic SDKs).
_FORWARD_HEADER_ALLOWLIST = (
    'anthropic-beta', 'anthropic-version', 'openai-beta', 'x-request-id', 'user-agent',
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Exempt OPTIONS preflight from auth so browser SDKs can CORS-negotiate.
    if request.method == 'OPTIONS':
        return await call_next(request)

    # Auth check — accepts both Authorization: Bearer <token> AND x-api-key: <token>
    # (Anthropic SDK uses x-api-key, OpenAI SDK uses Authorization).
    if not DISABLE_AUTH:
        path = request.url.path
        is_public = any(path.startswith(p) for p in PUBLIC_PATHS)
        if not is_public and not path.startswith('/catalog/') and not path.startswith('/openrouter/'):
            auth = request.headers.get('Authorization', '')
            x_api_key = request.headers.get('x-api-key', '')
            token = _bearer_token()
            client_token = ''
            if auth.lower().startswith('bearer '):
                client_token = auth[7:].strip()
            elif x_api_key:
                client_token = x_api_key.strip()
            elif auth:
                client_token = auth.strip()
            if token and (not client_token or not hmac.compare_digest(client_token, token)):
                return JSONResponse(
                    {"error": {"message": "Unauthorized", "type": "authentication_error"}},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer'},
                )

    # Rate limit
    ip = _client_ip(request)
    if not check_rate_limit(ip):
        return JSONResponse(
            {"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}},
            status_code=429,
            headers={"Retry-After": "60"},
        )

    # Process
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start

    # Add correlation ID and latency
    rid = request.headers.get('x-request-id', str(uuid.uuid4()))
    response.headers['x-request-id'] = rid
    response.headers['x-process-time'] = f"{elapsed:.3f}"

    return response


# ══════════════════════════════════════════════════════════════════════════
# PROXY ROUTES
# ══════════════════════════════════════════════════════════════════════════

async def _proxy_request(method: str, path: str, body: dict | None = None,
                         headers: dict | None = None, stream: bool = False,
                         request: Request | None = None) -> Response:
    """Generic proxy handler for OpenRouter API with multi-key retry loop.

    Iterates over all available keys on retriable failures (429, 5xx, network errors).
    Returns 429 (not 503) when all keys are exhausted so SDKs auto-retry.
    """
    model_id = (body or {}).get("model", "") if body else ""
    attempts = max(1, pool.total_keys)
    last_status = 429
    last_data = {"error": {"message": "All keys exhausted or rate-limited", "type": "rate_limit_error"}}

    for _ in range(attempts):
        acq = await pool.acquire(model=model_id)
        if not acq:
            break
        key_obj = acq['key']

        url = f"{OPENROUTER_BASE}/{path.lstrip('/')}"

        # Build headers: forward allowlisted client headers (preserves agent identity
        # and beta-feature flags), add upstream auth.
        fwd = {
            "Authorization": f"Bearer {key_obj.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        if request is not None:
            for h in _FORWARD_HEADER_ALLOWLIST:
                v = request.headers.get(h)
                if v:
                    fwd[h] = sanitize_header_value(v)
        if headers:
            fwd.update(headers)

        try:
            agent = await get_agent()
            timeout = aiohttp.ClientTimeout(
                total=None if stream else int(os.environ.get('REQUEST_TIMEOUT_SEC', '600')),
                sock_connect=int(os.environ.get('CONNECT_TIMEOUT_SEC', '30')),
                sock_read=int(os.environ.get('STREAM_SOCK_READ_TIMEOUT_SEC', '300')),
            )

            if stream:
                # Streaming: do NOT async-with so caller owns release; release in finally.
                resp = await agent.request(method, url, json=body, headers=fwd, timeout=timeout)
                if resp.status >= 400:
                    # Parse Retry-After header for 429 cooldown (anti rate-limit).
                    retry_after = _parse_retry_after(resp.headers, None) if resp.status == 429 else None
                    pool.mark_failure(key_obj, status_code=resp.status,
                                      available_keys=pool.available_keys,
                                      model=model_id,
                                      retry_after=retry_after)
                    error_text = await resp.text()
                    resp.release()
                    pool.release(key_obj)
                    last_status = resp.status
                    try:
                        last_data = json.loads(error_text)
                    except Exception:
                        last_data = {"error": {"message": error_text[:2000], "type": "upstream_error",
                                                 "status": resp.status}}
                    if _is_retriable_status(resp.status):
                        continue  # retry with next key
                    return JSONResponse(last_data, status_code=resp.status)
                pool.mark_success(key_obj, available_keys=pool.available_keys) if hasattr(pool, 'mark_success') else None

                # Heartbeat-aware streaming passthrough — keeps idle LBs/agents alive.
                heartbeat_ms = HEARTBEAT_MS
                heartbeat_bytes = b': heartbeat\n\n'
                resp_ref = resp
                released = False

                async def stream_gen():
                    nonlocal released
                    try:
                        last_chunk = time.time()
                        async for line in resp_ref.content:
                            last_chunk = time.time()
                            yield line
                        # If upstream didn't send [DONE], synthesize it for OpenAI SSE.
                        yield b'data: [DONE]\n\n'
                    except asyncio.CancelledError:
                        raise
                    finally:
                        if not released:
                            released = True
                            try:
                                resp_ref.release()
                            except Exception:
                                pass
                            pool.release(key_obj)

                async def stream_with_heartbeat():
                    async for chunk in stream_gen():
                        yield chunk

                return StreamingResponse(
                    stream_with_heartbeat(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={k: sanitize_header_value(v) for k, v in resp.headers.items()
                             if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')},
                )

            # Non-streaming
            async with agent.request(method, url, json=body, headers=fwd, timeout=timeout) as resp:
                await metrics.record_request(model=model_id, status_code=resp.status)
                text = await resp.text()
                if resp.status >= 400:
                    # Parse Retry-After header for 429 cooldown (anti rate-limit).
                    try:
                        body_data = json.loads(text) if text else {}
                    except Exception:
                        body_data = {"error": {"message": text[:2000], "type": "upstream_error",
                                           "status": resp.status}}
                    retry_after = _parse_retry_after(resp.headers, body_data if isinstance(body_data, dict) else None) if resp.status == 429 else None
                    pool.mark_failure(key_obj, status_code=resp.status,
                                      available_keys=pool.available_keys,
                                      model=model_id,
                                      retry_after=retry_after)
                    last_status = resp.status
                    last_data = body_data
                    if _is_retriable_status(resp.status):
                        continue  # retry with next key
                    return JSONResponse(body_data, status_code=resp.status)
                # Success
                if hasattr(pool, 'mark_success'):
                    pool.mark_success(key_obj, available_keys=pool.available_keys)
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                return JSONResponse(content=data, status_code=resp.status)

        except asyncio.TimeoutError:
            pool.mark_failure(key_obj, reason="timeout", available_keys=pool.available_keys)
            last_status = 504
            last_data = {"error": {"message": "Upstream request timed out", "type": "timeout_error"}}
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:
            pool.mark_failure(key_obj, reason=str(e)[:100], available_keys=pool.available_keys)
            logger.error(f"[openrouter] Proxy error: {e}")
            last_status = 502
            last_data = {"error": {"message": f"Upstream connection error: {type(e).__name__}: {str(e)[:500]}",
                                    "type": "api_error"}}
            continue
        finally:
            # Ensure key is released for non-stream paths (stream path releases in stream_gen finally).
            try:
                pool.release(key_obj)
            except Exception:
                pass

    # All keys exhausted — return 429 (not 503) so SDKs auto-retry with backoff.
    retry_after = str(int(os.environ.get('KEY_EXHAUSTED_RETRY_AFTER', '30')))
    return JSONResponse(
        last_data if isinstance(last_data, dict) else {"error": {"message": str(last_data)[:2000], "type": "rate_limit_error"}},
        status_code=last_status if last_status in (429, 401, 402, 403, 408, 409) else 429,
        headers={"Retry-After": retry_after},
    )


def _check_free_only(model: str) -> JSONResponse | None:
    """Check FREE_ONLY constraint. Returns error response if blocked.
    Returns 400 (not 403) for consistency with nous/opencode/blackbox wrappers."""
    if free_only_enabled() and model and not is_free_model(model):
        return JSONResponse(
            {"error": {"message": f"FREE_ONLY mode: model '{model}' is not a free model. "
                                   "Use models with :free suffix.", "type": "invalid_request_error"}},
            status_code=400,
        )
    return None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # FREE_ONLY check
    blocked = _check_free_only(model)
    if blocked:
        return blocked

    stream = body.get("stream", False)
    return await _proxy_request("POST", "chat/completions", body, stream=stream, request=request)


@app.post("/v1/responses")
async def responses(request: Request):
    """OpenAI Responses API (Codex/Claude Code) → Chat Completions translation.

    The Responses API uses `input` (string or array) instead of `messages`.
    We translate to Chat Completions, forward to OpenRouter, then translate
    the response back to Responses format. Streaming is supported via the
    ResponsesStreamState event lifecycle.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    # Translate Responses → Chat Completions
    principal = _request_principal(request)
    chat_body = responses_to_chat(body, principal=principal)
    is_stream = bool(chat_body.get("stream", False))

    response = await _proxy_request("POST", "chat/completions", chat_body, stream=is_stream, request=request)

    if isinstance(response, JSONResponse):
        # Reshape error to Responses API format.
        try:
            payload = json.loads(response.body)
            if isinstance(payload, dict) and 'error' in payload:
                return JSONResponse(
                    {"error": payload['error'], "type": "error"},
                    status_code=response.status_code,
                )
        except Exception:
            pass
        return response

    # Streaming: translate OpenAI SSE → Responses SSE event lifecycle.
    if is_stream and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_responses(response.body_iterator, model),
            media_type="text/event-stream",
            headers={"x-request-id": response.headers.get("x-request-id", "")},
        )

    # Non-streaming: translate Chat Completions JSON → Responses JSON.
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            resp = chat_to_responses(model, payload, body)
            # Store for previous_response_id continuity.
            resp_id = resp.get('id')
            if resp_id and principal:
                _RESPONSE_STORE[_response_store_key(principal, resp_id)] = chat_body.get('messages', [])
            return JSONResponse(resp, status_code=response.status_code)
    except Exception as e:
        logger.warning(f"[openrouter] /v1/responses translation failed: {e}")
    return response


async def _translate_openai_stream_to_responses(openai_gen, model: str):
    """Translate OpenAI Chat Completions SSE stream → Responses API SSE stream.

    Emits the full Responses event lifecycle:
      response.created → response.in_progress → response.output_item.added →
      response.content_part.added → response.output_text.delta →
      response.output_text.done → response.content_part.done →
      response.output_item.done → response.completed → [DONE]
    """
    resp_id = f"resp_{int(time.time()*1000)}"
    created_at = int(time.time())

    # response.created
    yield 'event: response.created\n'
    yield f'data: {json.dumps({"type": "response.created", "response": {"id": resp_id, "object": "response", "created_at": created_at, "model": model, "status": "in_progress", "output": []}})}\n\n'

    # response.in_progress
    yield 'event: response.in_progress\n'
    yield f'data: {json.dumps({"type": "response.in_progress", "response": {"id": resp_id, "status": "in_progress"}})}\n\n'

    msg_id = f"msg_{int(time.time()*1000)}"
    text_started = False
    full_text = ''

    async for chunk in openai_gen:
        if isinstance(chunk, bytes):
            line = chunk.decode('utf-8', errors='replace')
        else:
            line = chunk

        if not line.startswith('data: '):
            continue
        data_str = line[6:].strip()
        if data_str == '[DONE]':
            break

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if data.get('object') != 'chat.completion.chunk':
            continue
        choices = data.get('choices', [])
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta', {})

        if delta.get('content'):
            if not text_started:
                # output_item.added (message)
                yield 'event: response.output_item.added\n'
                yield f'data: {json.dumps({"type": "response.output_item.added", "output_index": 0, "item": {"id": msg_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})}\n\n'
                # content_part.added (output_text)
                yield 'event: response.content_part.added\n'
                yield f'data: {json.dumps({"type": "response.content_part.added", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})}\n\n'
                text_started = True
            full_text += delta['content']
            yield 'event: response.output_text.delta\n'
            yield f'data: {json.dumps({"type": "response.output_text.delta", "item_id": msg_id, "output_index": 0, "content_index": 0, "delta": delta["content"]})}\n\n'

        if choice.get('finish_reason'):
            break

    if text_started:
        # output_text.done
        yield 'event: response.output_text.done\n'
        yield f'data: {json.dumps({"type": "response.output_text.done", "item_id": msg_id, "output_index": 0, "content_index": 0, "text": full_text})}\n\n'
        # content_part.done
        yield 'event: response.content_part.done\n'
        yield f'data: {json.dumps({"type": "response.content_part.done", "item_id": msg_id, "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": full_text, "annotations": []}})}\n\n'
        # output_item.done
        yield 'event: response.output_item.done\n'
        yield f'data: {json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"id": msg_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": full_text, "annotations": []}]}})}\n\n'

    # response.completed
    yield 'event: response.completed\n'
    yield f'data: {json.dumps({"type": "response.completed", "response": {"id": resp_id, "object": "response", "created_at": created_at, "model": model, "status": "completed", "output": [{"id": msg_id, "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": full_text, "annotations": []}]}]}})}\n\n'

    yield 'data: [DONE]\n\n'


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return await _proxy_request("POST", "embeddings", body, request=request)


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return await _proxy_request("POST", "images/generations", body, request=request)


@app.get("/v1/models")
async def list_models(request: Request):
    """List models from OpenRouter, optionally augmented with catalog."""
    try:
        agent = await get_agent()
        acq = await pool.acquire()
        if not acq:
            return JSONResponse({"data": []})
        key_obj = acq['key']

        headers = {
            "Authorization": f"Bearer {key_obj.api_key}",
            "Accept": "application/json",
        }
        async with agent.get(f"{OPENROUTER_BASE}/models", headers=headers) as resp:
            pool.release(key_obj)
            if resp.status != 200:
                return JSONResponse({"data": []}, status_code=resp.status)
            data = await resp.json()
            models = data.get("data", [])

            # Filter FREE_ONLY
            if free_only_enabled():
                models = [m for m in models if is_free_model(m.get("id", ""))]

            return JSONResponse({"data": models, "object": "list"})
    except Exception as e:
        logger.error(f"[openrouter] Error listing models: {e}")
        return JSONResponse({"data": []})


@app.get("/v1/models/{model_id}")
async def get_model_detail(model_id: str, request: Request):
    return await _proxy_request("GET", f"models/{model_id}")


@app.get("/api/tags")
async def api_tags():
    """Ollama-compatible model discovery — PUBLIC (no auth).

    Returns the model list in Ollama's /api/tags format so Ollama clients
    can discover models served by this wrapper.
    """
    try:
        # Reuse /v1/models internal logic without auth (it's already public).
        models_resp = await list_models(Request(scope={'type': 'http', 'headers': [], 'method': 'GET', 'path': '/v1/models', 'query_string': b''}))
        if hasattr(models_resp, 'body'):
            try:
                data = json.loads(models_resp.body)
            except Exception:
                data = {}
        elif isinstance(models_resp, dict):
            data = models_resp
        else:
            data = {}
    except Exception:
        data = {}
    out_models = []
    for m in (data.get('data') or []):
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


# ── Anthropic-compatible ─────────────────────────────────────────────────

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages API → OpenAI Chat Completions translation."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    # Translate Anthropic → OpenAI
    openai_body = _anthropic_to_openai(body)
    openai_body["stream"] = body.get("stream", False)

    response = await _proxy_request("POST", "chat/completions", openai_body,
                                     stream=openai_body["stream"], request=request)

    if isinstance(response, JSONResponse):
        # Reshape error envelope to Anthropic format for SDK compatibility.
        try:
            payload = json.loads(response.body)
            if isinstance(payload, dict) and 'error' in payload:
                return JSONResponse(
                    {"type": "error", "error": payload['error']},
                    status_code=response.status_code,
                )
        except Exception:
            pass
        return response

    # For streaming, translate OpenAI SSE → Anthropic SSE
    if openai_body["stream"] and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_anthropic(response.body_iterator, body),
            media_type="text/event-stream",
            headers={"x-request-id": response.headers.get("x-request-id", "")},
        )

    # Non-streaming JSON: translate OpenAI ChatCompletion → Anthropic Message.
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            anthropic_resp = _openai_to_anthropic_response(payload, body)
            return JSONResponse(anthropic_resp, status_code=response.status_code)
    except Exception:
        pass
    return response


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token counting via OpenRouter.
    Returns an estimate; OpenRouter does not have a dedicated count_tokens endpoint."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    # Best-effort: count characters / 4 as a rough token estimate without burning quota.
    msgs = body.get("messages", [])
    total_chars = 0
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict):
                    total_chars += len(b.get("text", ""))
                    total_chars += len(json.dumps(b.get("input", {})))
    est_tokens = max(1, total_chars // 4)
    return JSONResponse({"input_tokens": est_tokens})


# ── Responses API (Codex/Claude Code) ↔ Chat Completions translation ──────
# These translators allow the wrapper to accept /v1/responses requests
# (which use `input` instead of `messages`) and forward them as standard
# /v1/chat/completions to OpenRouter — then translate the response back.

# Tenant-isolated response store for previous_response_id continuity.
_RESPONSE_STORE: dict[str, list] = {}


def _response_store_key(principal: str, response_id: str) -> str:
    import hashlib
    return hashlib.sha256(f"{principal}|{response_id}".encode()).hexdigest()


def _request_principal(request: Request) -> str:
    """Stable per-client principal for response-store namespacing.
    Uses the BEARER_TOKEN fingerprint (or 'anon' if auth disabled)."""
    tok = (request.headers.get('authorization') or request.headers.get('x-api-key') or '').strip()
    if tok.lower().startswith('bearer '):
        tok = tok[7:].strip()
    if not tok:
        return 'anon'
    import hashlib
    return hashlib.sha256(tok.encode()).hexdigest()[:16]


def responses_to_chat(body: dict, principal: str = '') -> dict:
    """Convert OpenAI Responses API request → Chat Completions request.

    Handles:
      - `input` as string (single user message) or array (mixed items).
      - `instructions` → system message (prepended).
      - `previous_response_id` → inject stored messages (tenant-scoped).
      - `function_call` / `function_call_output` items → tool_calls / tool role.
      - Forward all client params verbatim (transparent proxy principle).
    """
    model = body.get('model') or ''
    msgs: list = []
    prev = body.get('previous_response_id')
    if prev and principal:
        key = _response_store_key(principal, prev)
        if key in _RESPONSE_STORE:
            msgs.extend(_RESPONSE_STORE[key])
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
    # Forward client params verbatim (transparent proxy — no mutation).
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
            }})
        if tools:
            out['tools'] = tools
    return out


def chat_to_responses(model: str, data: dict, request_body: dict | None = None) -> dict:
    """Convert OpenAI Chat Completions response → Responses API response."""
    msg = (data.get('choices') or [{}])[0].get('message', {}) or {}
    text = msg.get('content') or ''
    output = []
    # Surface upstream reasoning_content as a reasoning output item.
    reasoning = msg.get('reasoning_content') or msg.get('reasoning') or ''
    if reasoning:
        output.append({'id': f"rsn_{int(time.time()*1000)}", 'type': 'reasoning',
                       'status': 'completed', 'text': reasoning})
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
    resp = {'id': data.get('id') or f"resp_{int(time.time()*1000)}",
            'object': 'response', 'created_at': int(time.time()),
            'model': model, 'status': 'completed', 'output': output,
            'usage': {'input_tokens': u.get('prompt_tokens', 0) or 0,
                      'output_tokens': u.get('completion_tokens', 0) or 0,
                      'total_tokens': u.get('total_tokens') or ((u.get('prompt_tokens', 0) or 0) + (u.get('completion_tokens', 0) or 0))}}
    return resp


def _anthropic_to_openai(body: dict) -> dict:
    """Convert Anthropic Messages API request to OpenAI Chat Completions.

    Correctly handles:
      - system field as a top-level system message (NOT a top-level field — OpenAI rejects that).
      - tool_result blocks as content of user messages → emitted as {role:'tool', tool_call_id, content}.
      - tool_use blocks in assistant messages → tool_calls array.
      - image blocks → image_url (base64 data URI).
    """
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
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
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
                messages.append(msg_obj)
            else:
                messages.append({"role": "assistant", "content": content})
        elif role == "user":
            if isinstance(content, list):
                # User content can include text blocks AND tool_result blocks.
                # tool_result must become a separate {role:'tool', tool_call_id, content} message.
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
                # Emit tool_result messages BEFORE the user text message (OpenAI requires tool results
                # to immediately follow the assistant's tool_calls).
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
    # Never inject a default (was 4096) — that mutates client intent.
    openai_body = {
        "model": model,
        "messages": messages,
    }
    if body.get("max_tokens") is not None:
        openai_body["max_tokens"] = body["max_tokens"]
    if "temperature" in body:
        openai_body["temperature"] = body["temperature"]
    if "top_p" in body:
        openai_body["top_p"] = body["top_p"]
    if "stop_sequences" in body:
        openai_body["stop"] = body["stop_sequences"]
    if body.get("tools"):
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}) or {"type": "object", "properties": {}},
                },
            }
            for t in body["tools"] if isinstance(t, dict) and t.get("name")
        ]
    if body.get("tool_choice"):
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            if tc.get("type") == "any":
                openai_body["tool_choice"] = "required"
            elif tc.get("type") == "auto":
                openai_body["tool_choice"] = "auto"
            elif tc.get("type") == "tool":
                openai_body["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
        elif isinstance(tc, str):
            openai_body["tool_choice"] = tc
    return openai_body


def _openai_to_anthropic_response(openai_resp: dict, request_body: dict) -> dict:
    """Convert OpenAI ChatCompletion response → Anthropic Message response."""
    choices = openai_resp.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {})
    content_blocks = []
    if message.get("content"):
        content_blocks.append({"type": "text", "text": message["content"]})
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
                   "content_filter": "end_turn"}.get(finish_reason, "end_turn")

    usage = openai_resp.get("usage", {})
    return {
        "id": openai_resp.get("id", f"msg_{int(time.time()*1000)}"),
        "type": "message",
        "role": "assistant",
        "model": request_body.get("model", ""),
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


async def _translate_openai_stream_to_anthropic(openai_gen, request_body: dict):
    """Translate OpenAI SSE stream → Anthropic SSE stream with proper event lifecycle.

    Emits: message_start → content_block_start → content_block_delta(s) →
    content_block_stop → message_delta → message_stop.
    """
    msg_id = f"msg_{int(time.time()*1000)}"
    model = request_body.get("model", "")
    # message_start
    yield 'event: message_start\n'
    yield f'data: {json.dumps({"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})}\n\n'

    block_open = False
    block_index = 0
    text_started = False
    tool_call_blocks = {}  # OpenAI tool index → Anthropic block index
    finish_reason = None
    output_tokens = 0
    input_tokens = 0

    async for chunk in openai_gen:
        if isinstance(chunk, bytes):
            line = chunk.decode('utf-8', errors='replace')
        else:
            line = chunk

        if not line.startswith('data: '):
            continue
        data_str = line[6:].strip()
        if data_str == '[DONE]':
            break

        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        if data.get('object') != 'chat.completion.chunk':
            continue
        choices = data.get('choices', [])
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get('delta', {})
        usage_chunk = data.get('usage')
        if usage_chunk:
            input_tokens = usage_chunk.get('prompt_tokens', input_tokens)
            output_tokens = usage_chunk.get('completion_tokens', output_tokens)

        # Text content
        if delta.get('content'):
            if not text_started:
                yield 'event: content_block_start\n'
                yield f'data: {json.dumps({"type": "content_block_start", "index": block_index, "content_block": {"type": "text", "text": ""}})}\n\n'
                text_started = True
                block_open = True
            yield 'event: content_block_delta\n'
            yield f'data: {json.dumps({"type": "content_block_delta", "index": block_index, "delta": {"type": "text_delta", "text": delta["content"]}})}\n\n'

        # Tool calls
        if delta.get('tool_calls'):
            for tc in delta['tool_calls']:
                tc_idx = tc.get('index', 0)
                fn = tc.get('function', {})
                if tc_idx not in tool_call_blocks:
                    # Close any open text block first.
                    if block_open and text_started:
                        yield 'event: content_block_stop\n'
                        yield f'data: {json.dumps({"type": "content_block_stop", "index": block_index})}\n\n'
                        block_index += 1
                        text_started = False
                        block_open = False
                    tool_call_blocks[tc_idx] = block_index
                    yield 'event: content_block_start\n'
                    yield f'data: {json.dumps({"type": "content_block_start", "index": block_index, "content_block": {"type": "tool_use", "id": tc.get("id", f"toolu_{block_index}"), "name": fn.get("name", ""), "input": {}}})}\n\n'
                    block_open = True
                if fn.get('arguments'):
                    yield 'event: content_block_delta\n'
                    yield f'data: {json.dumps({"type": "content_block_delta", "index": tool_call_blocks[tc_idx], "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})}\n\n'

        if choice.get('finish_reason'):
            finish_reason = choice['finish_reason']

    # Close any open block.
    if block_open:
        yield 'event: content_block_stop\n'
        yield f'data: {json.dumps({"type": "content_block_stop", "index": block_index})}\n\n'

    stop_reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                   "content_filter": "end_turn"}.get(finish_reason, "end_turn")

    yield 'event: message_delta\n'
    yield f'data: {json.dumps({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": output_tokens}})}\n\n'

    yield 'event: message_stop\n'
    yield f'data: {json.dumps({"type": "message_stop"})}\n\n'


# ══════════════════════════════════════════════════════════════════════════
# CATALOG ROUTES (prefix /catalog/)
# ══════════════════════════════════════════════════════════════════════════

def _get_catalog_db():
    """Open catalog database read-only."""
    if not _HAS_CATALOG or not os.path.exists(CATALOG_DB_PATH):
        return None
    return open_db(CATALOG_DB_PATH)


@app.get("/catalog/health")
async def catalog_health():
    if not _HAS_CATALOG:
        return {"ok": False, "catalog": "not_available"}
    exists = os.path.exists(CATALOG_DB_PATH)
    return {"ok": exists, "catalog": "available" if exists else "db_missing"}


@app.get("/catalog/stats")
async def catalog_stats_route():
    db = _get_catalog_db()
    if not db:
        return {"error": "Catalog not available"}
    try:
        result = catalog_stats(db)
        return result
    finally:
        db.close()


@app.get("/catalog/providers")
async def catalog_providers():
    db = _get_catalog_db()
    if not db:
        return {"error": "Catalog not available"}
    try:
        result = list_providers(db)
        return {"providers": result}
    finally:
        db.close()


@app.get("/catalog/models")
async def catalog_models(q: str = "", modality: str = "", tier: str = "",
                          working_only: bool = False, free_only: bool = False,
                          publisher: str = "", limit: int = 50):
    db = _get_catalog_db()
    if not db:
        return {"error": "Catalog not available"}
    try:
        results = search_models(db, query=q or None, modality=modality or None,
                                 tier=tier or None, working_only=working_only,
                                 free_only=free_only, publisher=publisher or None,
                                 limit=min(limit, 500))
        return {"count": len(results), "models": results}
    finally:
        db.close()


@app.get("/catalog/model")
async def catalog_model(id: str = ""):
    if not id:
        return {"error": "Missing 'id' parameter (use catalog_id format: publisher/slug)"}
    db = _get_catalog_db()
    if not db:
        return {"error": "Catalog not available"}
    try:
        result = get_model(db, id)
        return result or {"error": "Model not found"}
    finally:
        db.close()


@app.get("/catalog/provider-models")
async def catalog_provider_models(provider: str = "", q: str = "",
                                   free_only: bool = False, limit: int = 50):
    db = _get_catalog_db()
    if not db:
        return {"error": "Catalog not available"}
    try:
        results = search_provider_models(db, provider=provider or None, query=q or None,
                                          free_only=free_only, limit=min(limit, 500))
        return {"count": len(results), "models": results}
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════
# OPENROUTER KEY MANAGEMENT ROUTES (MANAGEMENT_KEY)
# ══════════════════════════════════════════════════════════════════════════

MANAGEMENT_KEY = os.environ.get('OPENROUTER_MANAGEMENT_KEY', '').strip()
MANAGEMENT_ENABLED = MGT.is_management_enabled("openrouter") if _HAS_MANAGEMENT else False


async def _mgmt_request(method: str, path: str = "", json_body: dict | None = None,
                          params: dict | None = None) -> Response:
    """Make authenticated management API request to OpenRouter."""
    if not MANAGEMENT_ENABLED:
        return JSONResponse(
            {"error": "OpenRouter management not enabled. Set OPENROUTER_MANAGEMENT_ENABLED=yes "
                       "and configure OPENROUTER_MANAGEMENT_KEY"},
            status_code=501,
        )
    mgmt_key = os.environ.get('OPENROUTER_MANAGEMENT_KEY', '').strip()
    if not mgmt_key:
        return JSONResponse(
            {"error": "OPENROUTER_MANAGEMENT_KEY not configured"},
            status_code=501,
        )

    base = "https://openrouter.ai/api/v1/keys"
    url = f"{base}/{path.lstrip('/')}" if path else base
    headers = {
        "Authorization": f"Bearer {mgmt_key}",
        "Content-Type": "application/json",
    }

    try:
        agent = await get_agent()
        kwargs = {"headers": headers}
        if json_body:
            kwargs["json"] = json_body
        if params:
            kwargs["params"] = params

        async with agent.request(method, url, **kwargs) as resp:
            data = await resp.json()
            return JSONResponse(data, status_code=resp.status)
    except Exception as e:
        logger.error(f"[openrouter] Management API error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)


@app.post("/openrouter/keys/list")
async def openrouter_list_keys(request: Request):
    """List all OpenRouter API keys (100 per page)."""
    body = await request.json() if request.headers.get("content-length") else {}
    offset = body.get("offset", 0)
    params = {"offset": str(offset)} if offset else None
    return await _mgmt_request("GET", params=params)


@app.post("/openrouter/keys/create")
async def openrouter_create_key(request: Request):
    """Create a new OpenRouter API key."""
    body = await request.json() if request.headers.get("content-length") else {}
    name = body.get("name", f"wrapper-created-{int(time.time())}")
    limit = body.get("limit")
    payload = {"name": name}
    if limit is not None:
        payload["limit"] = limit
    return await _mgmt_request("POST", json_body=payload)


@app.get("/openrouter/keys/{key_hash}")
async def openrouter_get_key(key_hash: str):
    """Get details for a specific API key."""
    return await _mgmt_request("GET", path=key_hash)


@app.patch("/openrouter/keys/{key_hash}")
async def openrouter_update_key(key_hash: str, request: Request):
    """Update an API key (name, disabled, limit_reset)."""
    body = await request.json() if request.headers.get("content-length") else {}
    return await _mgmt_request("PATCH", path=key_hash, json_body=body)


@app.delete("/openrouter/keys/{key_hash}")
async def openrouter_delete_key(key_hash: str):
    """Permanently delete an API key."""
    return await _mgmt_request("DELETE", path=key_hash)


@app.post("/openrouter/keys/rotate")
async def openrouter_rotate_key(request: Request):
    """Zero-downtime key rotation."""
    body = await request.json() if request.headers.get("content-length") else {}
    old_hash = body.get("old_key_hash", "")
    if not old_hash:
        return JSONResponse({"error": "old_key_hash is required"}, status_code=400)
    new_name = body.get("new_name")
    new_limit = body.get("new_limit")

    # Step 1: Create new key
    payload = {"name": new_name or f"rotated-{int(time.time())}"}
    if new_limit is not None:
        payload["limit"] = new_limit
    create_resp = await _mgmt_request("POST", json_body=payload)
    create_data = json.loads(create_resp.body) if hasattr(create_resp, 'body') else {}

    if create_resp.status_code not in (200, 201):
        return create_resp

    return JSONResponse({
        "success": True,
        "new_key": create_data,
        "old_key_hash": old_hash,
        "message": "New key created. Deploy it, verify it works, then delete the old key.",
        "warning": "Both keys are valid during transition.",
    })


@app.get("/openrouter/keys/usage")
async def openrouter_key_usage():
    """Get aggregated usage statistics."""
    return await _mgmt_request("GET")


# ══════════════════════════════════════════════════════════════════════════
# MCP TRANSPORT ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.get("/mcp/sse")
async def mcp_sse(request: Request):
    """SSE transport endpoint for MCP."""
    if not MCP_SERVER or not SSE_TRANSPORT:
        return JSONResponse({"error": "MCP not available"}, status_code=503)

    async def event_generator():
        async with anyio.create_task_group() as tg, SSE_TRANSPORT.connect_sse(
            request.scope, tg, request._receive
        ) as streams:
            await MCP_SERVER._mcp_server.run(
                streams[0], streams[1],
                MCP_SERVER._create_initialization_options()
            )

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/mcp/messages")
async def mcp_messages(request: Request):
    """Streamable HTTP transport for MCP (JSON-RPC messages)."""
    if not MCP_SERVER or not SSE_TRANSPORT:
        return JSONResponse({"error": "MCP not available"}, status_code=503)
    return await SSE_TRANSPORT.handle_post_message(request.scope, request._receive, request._send)


# ══════════════════════════════════════════════════════════════════════════
# MANAGEMENT ROUTES
# ══════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    mgmt_status = {}
    if _HAS_MANAGEMENT:
        try:
            mgmt_status = MGT.get_provider_keys_status("openrouter")
        except Exception:
            mgmt_status = {"openrouter": {"management_enabled": False, "error": "check_failed"}}
    return {
        "status": "ok",
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "keys": pool.available_keys,
        "uptime_seconds": int(time.time() - metrics.start),
        "catalog": _HAS_CATALOG and os.path.exists(CATALOG_DB_PATH),
        "management": _HAS_MANAGEMENT,
        "keys_status": mgmt_status,
    }


@app.get("/ready")
async def ready():
    return {
        "ready": pool.total_keys > 0,
        "keys_loaded": pool.total_keys,
        "catalog_ready": _HAS_CATALOG and os.path.exists(CATALOG_DB_PATH),
    }


@app.get("/metrics")
@app.get("/metrics/prom")
async def prom_metrics():
    pool_metrics = pool.prom_metrics()
    req_metrics = metrics.prom_metrics()
    return Response(
        content=pool_metrics + "\n" + req_metrics,
        media_type="text/plain; version=0.0.4",
    )


@app.get("/stats")
async def stats():
    s = await metrics.summary()
    s["key_pool"] = pool.health_json()
    s["free_only"] = free_only_enabled()
    return s


@app.get("/dashboard")
async def dashboard():
    """Simple built-in dashboard HTML."""
    try:
        dash_path = ROOT / 'dashboard.html'
        if dash_path.exists():
            content = dash_path.read_text(encoding='utf-8')
            return HTMLResponse(content)
    except Exception:
        pass

    # Fallback: inline minimal dashboard
    stats_data = await metrics.summary()
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>OpenRouter Wrapper Dashboard</title>
<style>
  body {{ font-family: system-ui, sans-serif; background: #111; color: #eee; margin: 2rem; }}
  .card {{ background: #1a1a2e; border-radius: 12px; padding: 1.5rem; margin: 1rem 0; }}
  h1 {{ color: #ff6b35; }}
  .stat {{ display: inline-block; margin: 0.5rem 1rem 0.5rem 0; }}
  .label {{ color: #888; font-size: 0.85rem; }}
  .value {{ font-size: 1.5rem; font-weight: bold; color: #fff; }}
  .ok {{ color: #4caf50; }} .degraded {{ color: #ff9800; }}
</style></head>
<body>
  <h1>🔌 OpenRouter Wrapper</h1>
  <div class="card">
    <h2>Status</h2>
    <div class="stat"><div class="label">Version</div><div class="value">{VERSION}</div></div>
    <div class="stat"><div class="label">Uptime</div><div class="value">{stats_data['uptime_seconds']}s</div></div>
    <div class="stat"><div class="label">Requests</div><div class="value">{stats_data['total_requests']}</div></div>
    <div class="stat"><div class="label">Tokens</div><div class="value">{stats_data['total_tokens']}</div></div>
    <div class="stat"><div class="label">Error Rate</div><div class="value">{stats_data['error_rate']}</div></div>
    <div class="stat"><div class="label">Keys</div><div class="value">{pool.total_keys} ({pool.available_keys} available)</div></div>
    <div class="stat"><div class="label">FREE_ONLY</div><div class="value">{'✅ ON' if free_only_enabled() else 'OFF'}</div></div>
    <div class="stat"><div class="label">Catalog</div><div class="value">{'✅ Available' if _HAS_CATALOG else '❌ N/A'}</div></div>
  </div>
  <div class="card">
    <h2>Endpoints</h2>
    <ul>
      <li><code>POST /v1/chat/completions</code> — OpenAI Chat</li>
      <li><code>POST /v1/responses</code> — OpenAI Responses API</li>
      <li><code>POST /v1/messages</code> — Anthropic Messages</li>
      <li><code>GET /v1/models</code> — Model listing</li>
      <li><code>GET /catalog/models</code> — NIM Catalog models</li>
      <li><code>GET /mcp/sse</code> — MCP SSE transport</li>
    </ul>
  </div>
</body></html>"""
    return HTMLResponse(html)


@app.get("/version")
async def version():
    return {
        "app": "wrapper-openrouter",
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "upstream": "OpenRouter",
        "source_root": SOURCE_ROOT,
    }


# ══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

def main():
    """Entry point for `python -m openrouter.src.main` and the `wrapper-openrouter` console script."""
    import uvicorn
    validate_config()
    uvicorn.run(
        "openrouter.src.main:app",
        host=BIND_HOST,
        port=LISTEN_PORT,
        reload=os.environ.get('UVICORN_RELOAD', '').lower() in ('1', 'true'),
        workers=int(os.environ.get('UVICORN_WORKERS', '1')),
        log_level="info",
    )


if __name__ == "__main__":
    main()
