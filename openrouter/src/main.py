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

import os
import sys
import json
import hmac
import time
import uuid
import asyncio
import logging
import threading
from pathlib import Path
from typing import Optional, Set, AsyncGenerator
from contextlib import asynccontextmanager

# ── Shared monorepo imports ──────────────────────────────────────────────
try:
    from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.model_state import ModelStateStore, classify_upstream_error, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id

# ── Catalog MCP integration ──────────────────────────────────────────────
CATALOG_REPO = os.environ.get('CATALOG_REPO', str(Path(__file__).resolve().parents[2] / 'model_fetcher'))
if CATALOG_REPO not in sys.path:
    sys.path.insert(0, CATALOG_REPO)

try:
    from catalog_queries import search_models, get_model, list_providers, search_provider_models, \
        get_provider_model, stats as catalog_stats, open_db, DEFAULT_DB
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
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

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
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from .key_pool import KeyPool

# Circuit breaker for upstream protection
try:
    from common.circuit_breaker import CircuitBreaker, CircuitBreakerError
    _UPSTREAM_BREAKER = CircuitBreaker(failure_threshold=10, recovery_timeout=30, name="openrouter-upstream")
    _HAS_CIRCUIT_BREAKER = True
except ImportError:
    _HAS_CIRCUIT_BREAKER = False

from .metrics import Metrics

# ── Shared translations ─────────────────────────────────────────────────
try:
    from common.translations import (
        AnthropicStreamState,
        normalize_upstream_error as _normalize_upstream_error,
        strip_cache_control as _strip_cache,
        repair_orphan_tool_messages as _repair_orphan_tool_messages,
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
    """Validate required configuration at startup."""
    missing = []
    for var in ['OPENROUTER_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var) and not os.environ.get('DISABLE_AUTH'):
            missing.append(var)

    if not os.environ.get('DISABLE_AUTH') and missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        # Don't exit in library mode; allow env var checking at runtime

    try:
        port = int(os.environ.get('LISTEN_PORT', '9106'))
        if not (1024 <= port <= 65535):
            logger.error(f"Invalid port {port}")
    except ValueError:
        logger.error("LISTEN_PORT must be an integer")


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
    if mid.endswith(':free') or mid.endswith('-free'):
        return True
    return False


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
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "120"))


def check_rate_limit(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
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
            "model listings (OpenRouter, Nous, OpenCode, Blackbox, Vercel)."
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
_agent: Optional[aiohttp.ClientSession] = None


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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id", "x-process-time"],
)

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter, max_size=50 * 1024 * 1024)


# ── Auth Middleware ────────────────────────────────────────────────────────

DISABLE_AUTH = os.environ.get('DISABLE_AUTH', '').strip().lower() in ('1', 'true', 'yes')
PUBLIC_PATHS = {'/health', '/ready', '/metrics', '/metrics/prom', '/dashboard',
                '/catalog/health', '/catalog/ready', '/catalog/metrics',
                '/mcp/sse', '/mcp/messages', '/mcp'}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Auth check
    if not DISABLE_AUTH:
        path = request.url.path
        is_public = any(path.startswith(p) for p in PUBLIC_PATHS)
        if not is_public and not path.startswith('/catalog/'):
            auth = request.headers.get('Authorization', '')
            token = _bearer_token()
            if token and (not auth or not hmac.compare_digest(auth.replace('Bearer ', '').strip(), token)):
                return JSONResponse(
                    {"error": {"message": "Unauthorized", "type": "auth_error"}},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
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

async def _proxy_request(method: str, path: str, body: Optional[dict] = None,
                         headers: Optional[dict] = None, stream: bool = False) -> Response:
    """Generic proxy handler for OpenRouter API."""
    acq = await pool.acquire(model=(body or {}).get("model", "") if body else "")
    if not acq:
        return JSONResponse(
            {"error": {"message": "All keys exhausted or rate-limited", "type": "server_error"}},
            status_code=503,
        )
    key_obj = acq['key']

    url = f"{OPENROUTER_BASE}/{path.lstrip('/')}"

    req_headers = {
        "Authorization": f"Bearer {key_obj.api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "User-Agent": "wrapper-openrouter/1.0",
    }
    if headers:
        req_headers.update(headers)

    try:
        agent = await get_agent()

        async with agent.request(method, url, json=body, headers=req_headers) as resp:
            # Track metrics
            await metrics.record_request(
                model=(body or {}).get("model", ""),
                status_code=resp.status,
            )

            if resp.status >= 400:
                pool.mark_failure(key_obj, status_code=resp.status,
                                  available_keys=pool.available_keys)
                error_text = await resp.text()
                return JSONResponse(
                    {"error": {"message": error_text[:2000], "type": "upstream_error",
                               "status": resp.status}},
                    status_code=resp.status,
                )

            if stream:
                # Streaming response
                async def stream_gen():
                    async for line in resp.content:
                        yield line

                return StreamingResponse(
                    stream_gen(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={k: sanitize_header_value(v) for k, v in resp.headers.items()
                             if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')},
                )
            else:
                data = await resp.json()
                return JSONResponse(content=data, status_code=resp.status)

    except asyncio.TimeoutError:
        pool.mark_failure(key_obj, reason="timeout", available_keys=pool.available_keys)
        return JSONResponse(
            {"error": {"message": "Upstream request timed out", "type": "timeout_error"}},
            status_code=504,
        )
    except Exception as e:
        pool.mark_failure(key_obj, reason=str(e)[:100], available_keys=pool.available_keys)
        logger.error(f"[openrouter] Proxy error: {e}")
        return JSONResponse(
            {"error": {"message": "Internal proxy error", "type": "server_error"}},
            status_code=502,
        )
    finally:
        pool.release(key_obj)


def _check_free_only(model: str) -> Optional[JSONResponse]:
    """Check FREE_ONLY constraint. Returns error response if blocked."""
    if free_only_enabled() and model and not is_free_model(model):
        return JSONResponse(
            {"error": {"message": f"FREE_ONLY mode: model '{model}' is not a free model. "
                                   "Use models with :free suffix.", "type": "permission_error"}},
            status_code=403,
        )
    return None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "")

    # FREE_ONLY check
    blocked = _check_free_only(model)
    if blocked:
        return blocked

    stream = body.get("stream", False)
    return await _proxy_request("POST", "chat/completions", body, stream=stream)


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    model = body.get("model", "")

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    return await _proxy_request("POST", "responses", body, stream=body.get("stream", False))


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    return await _proxy_request("POST", "embeddings", body)


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    body = await request.json()
    return await _proxy_request("POST", "images/generations", body)


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


# ── Anthropic-compatible ─────────────────────────────────────────────────

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages API → OpenAI Chat Completions translation."""
    body = await request.json()
    model = body.get("model", "")

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    # Translate Anthropic → OpenAI
    openai_body = _anthropic_to_openai(body)
    openai_body["stream"] = body.get("stream", False)

    response = await _proxy_request("POST", "chat/completions", openai_body,
                                     stream=openai_body["stream"])

    if isinstance(response, JSONResponse):
        return response

    # For streaming, translate OpenAI SSE → Anthropic SSE
    if openai_body["stream"] and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_anthropic(response.body_iterator),
            media_type="text/event-stream",
            headers={"x-request-id": response.headers.get("x-request-id", "")},
        )

    return response


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Token counting via OpenRouter."""
    body = await request.json()
    return await _proxy_request("POST", "chat/completions", {
        "model": body.get("model", ""),
        "messages": body.get("messages", []),
        "max_tokens": 1,
    })


def _anthropic_to_openai(body: dict) -> dict:
    """Convert Anthropic Messages API request to OpenAI Chat Completions."""
    messages = []
    system = body.get("system", "")
    for msg in body.get("messages", []):
        role = msg["role"]
        if role == "assistant" and msg.get("content"):
            # Handle tool_use content blocks
            content = msg["content"]
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        })
                messages.append({
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else "",
                    "tool_calls": tool_calls if tool_calls else None,
                })
            else:
                messages.append({"role": "assistant", "content": content})
        elif role == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle image/file content blocks
                text_content = []
                for block in content:
                    if block.get("type") == "text":
                        text_content.append(block["text"])
                    elif block.get("type") == "image":
                        text_content.append(f"[Image: {block.get('source', {}).get('data', '')[:50]}...]")
                    elif block.get("type") == "tool_result":
                        text_content.append(json.dumps(block.get("content", "")))
                messages.append({"role": "user", "content": "\n".join(text_content)})
            else:
                messages.append({"role": "user", "content": content})
        elif role == "tool_result":
            messages.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_use_id", ""),
                "content": msg.get("content", ""),
            })
        else:
            messages.append({"role": role, "content": msg.get("content", "")})

    openai_body = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
    }
    if system:
        openai_body["system"] = system if isinstance(system, str) else "\n".join(
            [b["text"] for b in system if b.get("type") == "text"]
        )
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
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            }
            for t in body["tools"]
        ]
    return openai_body


async def _translate_openai_stream_to_anthropic(openai_gen):
    """Translate OpenAI SSE stream → Anthropic SSE stream."""
    async for chunk in openai_gen:
        if isinstance(chunk, bytes):
            line = chunk.decode('utf-8', errors='replace')
        else:
            line = chunk

        if line.startswith('data: '):
            data_str = line[6:].strip()
            if data_str == '[DONE]':
                yield 'data: {"type": "message_stop"}\n\n'
                continue

            try:
                data = json.loads(data_str)
                if data.get('object') == 'chat.completion.chunk':
                    delta = data.get('choices', [{}])[0].get('delta', {})
                    if delta.get('content'):
                        anthropic_chunk = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": delta["content"]},
                        }
                        yield f'data: {json.dumps(anthropic_chunk)}\n\n'
                    if delta.get('tool_calls'):
                        for tc in delta['tool_calls']:
                            yield f'data: {json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": {}}})}\n\n'
                            yield f'data: {json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": tc["function"]["arguments"]}})}\n\n'
                    if data.get('choices', [{}])[0].get('finish_reason'):
                        yield 'data: {"type": "message_stop"}\n\n'
            except json.JSONDecodeError:
                pass


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


async def _mgmt_request(method: str, path: str = "", json_body: dict = None,
                          params: dict = None) -> Response:
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
        async with anyio.create_task_group() as tg:
            async with SSE_TRANSPORT.connect_sse(
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

if __name__ == "__main__":
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
