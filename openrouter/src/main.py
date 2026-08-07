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
import copy
import secrets
import hmac
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
        # R18/B-18.1: fallback MUST match common.middleware.sanitize_header_value
        # byte-for-byte — the previous regex excluded \x0a/\x0d, so CR and LF
        # survived (CRLF header-injection hole in degraded mode).
        if not value:
            return value
        import re as _re
        sanitized = value.replace('\r', '').replace('\n', '')
        sanitized = _re.sub(r'[\x00-\x1f\x7f]', '', sanitized)
        return sanitized.strip()


# B-36.1: shared upstream-payload sanitizer — NaN/±Infinity literals (accepted
# by json.loads) are replaced with None at the ingest boundary so the
# response render (allow_nan=False) can never 500 on a successful turn.
try:
    from common.model.sanitize import sanitize_nonfinite_numbers as _sanitize_nonfinite
except ImportError:  # pragma: no cover - standalone fallback
    import math as _math_nonfinite

    def _sanitize_nonfinite(payload):  # type: ignore[misc]
        # B-36.1: twin of common.model.sanitize.sanitize_nonfinite_numbers (verbatim).
        if isinstance(payload, float):
            return payload if _math_nonfinite.isfinite(payload) else None
        if isinstance(payload, (dict, list)):
            # Single stack, typed nodes: dict mutates by key, list by index.
            # (The first version popped list frames through dict.items() — the
            # unit test caught it: mixed containers are the norm.)
            stack = [payload]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    for key, child in node.items():
                        if isinstance(child, float):
                            if not _math_nonfinite.isfinite(child):
                                node[key] = None
                        elif isinstance(child, (dict, list)):
                            stack.append(child)
                else:
                    for idx, child in enumerate(node):
                        if isinstance(child, float):
                            if not _math_nonfinite.isfinite(child):
                                node[idx] = None
                        elif isinstance(child, (dict, list)):
                            stack.append(child)
            return payload
        return payload

# B-08 fix: shared sentinel-task idle iterator + CRLF normalisation.
from common.sse import (  # noqa: E402
    IDLE as _IDLE,
    iter_chunks_with_idle as _iter_chunks_with_idle,
    normalize_sse_newlines as _normalize_sse_newlines,
)

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
        build_forward_headers as _build_forward_headers,
        sanitize_header_value,
        anthropic_to_openai_response,
        openai_to_anthropic_response,
        stream_anthropic_to_openai,
        openai_chat_to_anthropic_request,
        new_response_id as _new_response_id,
    )
    from common.compat import (
        is_anthropic_upstream as _is_anthropic_upstream,
        is_auto_discovery as _is_auto_discovery,
        resolve_upstream_is_anthropic as _resolve_upstream_is_anthropic,
        passthrough_anthropic_sse as _passthrough_anthropic_sse,
        translate_anthropic_stream_to_openai_chat as _translate_anthropic_stream_to_openai_chat,
        translate_openai_chat_sse_to_responses as _translate_openai_chat_sse_to_responses,
        probe_upstream_compatibility as _probe_upstream_compatibility,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError as _imp_err:
    raise RuntimeError("common.translations import failed; wrapper requires shared translations") from _imp_err


async def _upstream_is_anthropic() -> bool:
    # R17 (B-17.1): defer the dialect routing decision to the shared
    # resolver so COMPATIBILITY_LAYER=3 auto-discovery actually applies.
    return await _resolve_upstream_is_anthropic(get_agent, OPENROUTER_BASE)

# P0-4/P0-1 fixes (audit 2026-08-03): central special-token scrubbing +
# shape-aware passthrough re-serialisation.
try:
    from common.sanitize_tokens import (
        PassthroughBlockRewriter as _PassthroughBlockRewriter,
        PassthroughSSE as _PassthroughSSE,
        SpecialTokenFilter as _SpecialTokenFilter,
        DsmlMarkupFilter as _DsmlMarkupFilter,
        filter_special_tokens as _filter_special_tokens,
        strip_dsml_markup as _strip_dsml_markup,
        scrub_openai_response_inplace as _scrub_openai_response_inplace,
        sse_block as _shared_sse_block,
    )
except ImportError as _imp_err:
    raise RuntimeError("common.sanitize_tokens import failed; wrapper requires shared sanitizer") from _imp_err

# P1-1/P1-3 shared helpers (input_image passthrough, full Responses usage).
try:
    from common.translations.shared import (
        responses_content_to_chat as _responses_content_to_chat,
        tokens_from_chat_usage as _tokens_from_chat_usage,
        responses_usage as _responses_usage,
    )
except ImportError as _imp_err:
    raise RuntimeError("common.translations.shared import failed; wrapper requires shared helpers") from _imp_err

# ── MCP integration (FastMCP) ───────────────────────────────────────────
# P2 fix (audit 2026-08-03): `mcp>=2` renamed/removed the fastmcp shim, and a
# hard import crash killed the WHOLE wrapper at boot even though only the
# (optional) /mcp surface needs it. Degrade gracefully: the wrapper serves
# every inference surface and reports 501 on /mcp instead.
try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.sse import SseServerTransport
    _HAS_MCP = True
except ImportError:  # pragma: no cover - depends on installed mcp version
    _HAS_MCP = False

    class FastMCP:  # type: ignore[no-redef]
        """Stub so the wrapper boots without the optional mcp dependency."""

        def __init__(self, *a, **kw):
            pass

        def tool(self, *a, **kw):
            def _deco(fn):
                return fn
            return _deco

        def sse_app(self, *a, **kw):
            return None

    class SseServerTransport:  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            pass

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
    # COMPATIBILITY_LAYER: operator-declared upstream dialect (1=OpenAI,
    # 2=Anthropic, 3=Auto). Fail fast on invalid values so the wrapper never
    # guesses the upstream protocol.
    try:
        from common.compat import validate_compat_layer
        validate_compat_layer()
    except ValueError as _e:
        print(f"❌ ERROR: {_e}")
        sys.exit(1)
    except ImportError:
        pass
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
    """Rate limiting keys by the real peer. P2 fix (audit 2026-08-03): the
    client-supplied X-Forwarded-For fallback is removed — it was trivially
    spoofable, letting an attacker rotate values to bypass the limiter. When
    no peer is known everything shares one 'unknown' bucket."""
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


# P2 fix: when the optional `mcp` package is missing/incompatible, boot the
# wrapper without the /mcp surface (routes already return 503 via this None).
MCP_SERVER = _create_mcp_server() if _HAS_MCP else None
SSE_TRANSPORT = SseServerTransport("/mcp/messages") if MCP_SERVER else None


# ── Key Pool ──────────────────────────────────────────────────────────────
pool = KeyPool()
metrics = Metrics()


def _sse_frame(obj, event_name: str = None) -> bytes:
    """Serialise one SSE event block (event: line only when given)."""
    return _shared_sse_block(obj, event_name)


def _error_response(content, status_code: int = 500, headers: dict | None = None) -> JSONResponse:
    """Shaped error response that also counts the error in metrics (B-39).

    openrouter's `record_error()` was dead code — no caller ever invoked it,
    so local error responses (auth rejections, invalid JSON, FREE_ONLY
    blocks, pool exhaustion) never incremented the dashboard's error counter
    and `error_rate` reported false health. Every local error response now
    goes through this helper (opencode `_jr` parity); upstream errors are
    still counted by `metrics.record_request(status_code=...)`.
    """
    try:
        metrics.record_error(status_code=status_code)
    except Exception:
        pass
    if headers is not None:
        return JSONResponse(content, status_code=status_code, headers=headers)
    return JSONResponse(content, status_code=status_code)

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
    # B-22: `global _MODEL_REFRESH_TASK` removed — never assigned in this scope
    # (the task handle is set by lifespan, which declares it correctly).
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
            data = _sanitize_nonfinite(await resp.json())
            models = [m["id"] for m in data.get("data", [])]
            return models
    except Exception as e:
        logger.warning(f"[openrouter] Model refresh error: {e}")
        return []


# ── Background tasks ───────────────────────────────────────────────────────
# B-35 fix: openrouter was the only wrapper with no background-task registry.
# asyncio only holds a WEAK reference to a running task, so a fire-and-forget
# coroutine can be garbage-collected mid-flight. All four siblings retain
# strong refs (_BG_TASKS / _spawn_bg_task / _spawn_background); this is the
# openrouter equivalent.
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


async def _record_model_result(model_id: str, api_key: str, status: int, data, url: str):
    """Persist the account-scoped upstream outcome (parity fix).

    openrouter was the only wrapper that never recorded model-state
    observations, so its models were invisible to the shared model registry
    (MODEL_STORE / MODEL_REGISTRY_CLIENT were constructed but never written to).
    Runs as a background task so the SQLite commit never sits on the TTFB path.
    """
    try:
        if status == 200:
            stored = await MODEL_STORE.record_status_async(
                model_id, credential_fingerprint(api_key), 'available', status, 'OK', endpoint=url)
        else:
            stored = await MODEL_STORE.record_error_async(model_id, api_key, status, data, endpoint=url)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            'openrouter', model_id,
            stored.get('account_scope', credential_fingerprint(api_key)),
            stored.get('state', 'unknown'), status, stored.get('reason_code', ''),
            stored.get('reason_detail', ''), url,
        )
    except Exception as e:
        logger.warning(f'[model-state] openrouter result record failed: {e}')


async def _drain_background_tasks(timeout: float = 5.0):
    """Await outstanding background tasks during shutdown (B-35)."""
    if not _BG_TASKS:
        return
    pending = list(_BG_TASKS)
    logger.info('[openrouter] draining %d background task(s)', len(pending))
    try:
        await asyncio.wait(pending, timeout=timeout)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning('[openrouter] background drain error: %r', e)


# ── Lifecycle ──────────────────────────────────────────────────────────────

_HEAL_TASK_REF = [None]


async def _heal_in_flight_loop():
    """P2 fix (audit 2026-08-03, nvidia/nous parity): periodic heal of leaked
    in_flight slots (a crashed-between-acquire-and-release request path would
    otherwise permanently shrink effective pool capacity)."""
    interval = int(os.environ.get("HEAL_INFLIGHT_INTERVAL_SEC", "300"))
    while True:
        await asyncio.sleep(max(30, interval))
        try:
            await pool.heal_in_flight()
        except Exception as e:
            logger.warning(f"[key_pool] heal_in_flight loop error: {e}")


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
    _HEAL_TASK_REF[0] = asyncio.create_task(_heal_in_flight_loop())  # P2
    logger.info(f"[openrouter] Ready on {BIND_HOST}:{LISTEN_PORT}")

    yield

    # Shutdown
    # B-34 fix: drain in-flight requests before tearing down the session.
    # Previously openrouter closed the aiohttp session immediately, severing
    # every active stream mid-response on each deploy/restart (all three
    # siblings already implement this drain).
    logger.info("[openrouter] Starting graceful shutdown...")
    shutdown_start = time.time()
    max_wait = int(os.environ.get('SHUTDOWN_DRAIN_SEC', '30'))
    while shutdown_start + max_wait > time.time():
        total = sum(k.in_flight for k in pool.keys)
        if total == 0:
            logger.info("[openrouter] All requests drained")
            break
        await asyncio.sleep(0.1)
    else:
        logger.warning("[openrouter] Drain timeout — %d request(s) still in flight",
                       sum(k.in_flight for k in pool.keys))

    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass

    # B-35 fix: await outstanding fire-and-forget tasks so metrics/model-state
    # writes are not lost (and cannot be GC'd mid-flight).
    await _drain_background_tasks()

    await metrics.close()
    if _agent and not _agent.closed:
        await _agent.close()
    logger.info("[openrouter] Shutdown complete")


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


# R-01 fix: reject non-object JSON bodies with a shaped 400 instead of letting
# `body.get(...)` raise AttributeError -> HTTP 500 (see common/body_guard.py).
try:
    from common.body_guard import JSONBodyGuard as _JSONBodyGuard
    app.add_middleware(_JSONBodyGuard)
except ImportError:  # pragma: no cover
    pass

if _HAS_SIZE_LIMITER:
    # B-32 fix: align the request-size cap with every sibling wrapper
    # (common.middleware default = 10MB). openrouter previously allowed 50MB,
    # 5x the fleet-wide memory-exhaustion headroom.
    app.add_middleware(RequestSizeLimiter)


# ── Auth Middleware ────────────────────────────────────────────────────────

DISABLE_AUTH = os.environ.get('DISABLE_AUTH', '').strip().lower() in ('1', 'true', 'yes')

# B-28 fix: fail CLOSED when no bearer token is configured. A truncated/empty
# .env previously turned the proxy into an open relay burning upstream credit.
# Set REQUIRE_AUTH=false only for deliberately open LAN deployments.
REQUIRE_AUTH = os.environ.get('REQUIRE_AUTH', 'true').strip().lower() not in ('0', 'false', 'no', 'off')

# B-27 fix: exact-match public paths + explicit method gating. The previous
# `startswith` test made '/v1/models' match '/v1/models-anything' and ignored
# the HTTP method, so any future route sharing a public prefix (or a POST to a
# GET-only discovery path) was silently unauthenticated.
PUBLIC_PATHS_ANY = frozenset({
    '/health', '/ready', '/metrics', '/metrics/prom', '/dashboard', '/stats',
    '/catalog/health', '/catalog/ready', '/catalog/metrics',
})
# Public model discovery (Ollama + OpenAI compatible) — agents need to list
# models before authenticating. GET only. MCP POST messages are deliberately
# absent here so every POST surface remains authenticated (B-31 parity).
PUBLIC_PATHS_GET = frozenset({'/api/tags', '/v1/models', '/version', '/mcp/sse', '/mcp'})

# B-26 fix: the OpenRouter *Provisioning* API (create/delete/rotate keys) is
# privileged and must never share the bypass with read-only catalog routes.
MANAGEMENT_PREFIX = '/openrouter/'


def _management_token() -> str:
    """Dedicated token for the key-management surface (B-26).

    Falls back to the inference bearer token so existing single-token
    deployments keep working, but the management routes are NEVER public.
    """
    return (os.environ.get('OPENROUTER_MANAGEMENT_TOKEN')
            or os.environ.get('MANAGEMENT_TOKEN')
            or _bearer_token()
            or '').strip()


def _is_public_path(path: str, method: str) -> bool:
    """Exact-match public-path test (B-27)."""
    if path in PUBLIC_PATHS_ANY:
        return True
    if method == 'GET' and path in PUBLIC_PATHS_GET:
        return True
    # Read-only catalog browsing stays public (GET only).
    if method == 'GET' and path.startswith('/catalog/'):
        return True
    return False


def _is_loopback_client(request: Request) -> bool:
    """Management API hardening (B-26): only local clients may administer keys."""
    host = getattr(request.client, 'host', '') if request.client else ''
    return host in ('127.0.0.1', '::1', 'localhost', 'testclient')

# Headers forwarded upstream (transparent passthrough to preserve client identity
# and beta-feature flags for OpenAI/Anthropic SDKs).
_FORWARD_HEADER_ALLOWLIST = (
    'anthropic-beta', 'anthropic-version', 'openai-beta', 'x-request-id', 'user-agent',
)



def _hdr_echo(v, max_len=128):
    """B-31.1: response-header-safe echo of an untrusted x-request-id
    (client- or upstream-supplied). Header values must be latin-1-encodable
    at send time — codepoints >255 raised UnicodeEncodeError → unhandled 500
    on the response path; control chars are equally illegal. Keep printable
    ASCII only, length-capped."""
    s = ''.join(ch for ch in str(v) if 32 <= ord(ch) <= 126)
    return s[:max_len] or 'unknown'


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Exempt OPTIONS preflight from auth so browser SDKs can CORS-negotiate.
    if request.method == 'OPTIONS':
        return await call_next(request)

    # Auth check — accepts both Authorization: Bearer <token> AND x-api-key: <token>
    # (Anthropic SDK uses x-api-key, OpenAI SDK uses Authorization).
    path = request.url.path
    method = request.method
    is_management = path.startswith(MANAGEMENT_PREFIX)
    if is_management and not _is_loopback_client(request):
        logger.warning('[auth] rejecting non-loopback management request from %s to %s',
                       getattr(request.client, 'host', None) if request.client else None, path)
        return _error_response(
            {"error": {"message": "OpenRouter management API is loopback-only", "type": "authentication_error"}},
            status_code=403,
        )
    # B-26 fix: management routes are NEVER public and NEVER inherit the
    # inference DISABLE_AUTH bypass. B-27/B-28 continue to apply to normal
    # inference routes unless the operator explicitly disables inference auth.
    if is_management or (not DISABLE_AUTH and not _is_public_path(path, method)):
        auth = request.headers.get('Authorization', '')
        x_api_key = request.headers.get('x-api-key', '')
        # B-26: privileged provisioning surface uses its own token.
        token = _management_token() if is_management else _bearer_token()
        # P0-2 fix: evaluate Authorization AND x-api-key as independent
        # candidates (nvidia V-19 / shared-auth parity). The Anthropic SDK can
        # send both — a stale Authorization value must not mask a valid
        # x-api-key, or a correctly configured client gets 401 on every call.
        candidates = []
        if auth.lower().startswith('bearer '):
            if auth[7:].strip():
                candidates.append(auth[7:].strip())
        elif auth.strip():
            candidates.append(auth.strip())
        if x_api_key.strip():
            candidates.append(x_api_key.strip())

        # B-28 fix: fail CLOSED when no token is configured instead of
        # silently allowing every request through.
        if not token:
            if is_management or REQUIRE_AUTH:
                logger.error(
                    '[auth] no %s token configured%s — refusing request to %s',
                    'management' if is_management else 'bearer',
                    ' (management never fails open)' if is_management else ' and REQUIRE_AUTH=true',
                    path,
                )
                return _error_response(
                    {"error": {"message": "Server auth not configured", "type": "authentication_error"}},
                    status_code=503,
                )
            logger.warning('[auth] token unset and REQUIRE_AUTH=false — serving %s OPEN (insecure)', path)
        else:
            # B-30 parity: compare as bytes so a non-ASCII token yields a
            # clean 401 instead of a TypeError → 500. P0-2: match ANY candidate.
            ok = any(
                hmac.compare_digest(c.encode('utf-8'), token.encode('utf-8'))
                for c in candidates)
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

    # Process
    start = time.time()
    response = await call_next(request)
    elapsed = time.time() - start

    # Add correlation ID and latency
    # B-31.1: sanitize the echo — untrusted ids can carry codepoints >255
    # (UnicodeEncodeError → 500 on the send path) or control chars.
    rid = request.headers.get('x-request-id', str(uuid.uuid4()))
    response.headers['x-request-id'] = _hdr_echo(rid)
    response.headers['x-process-time'] = f"{elapsed:.3f}"

    return response


# ══════════════════════════════════════════════════════════════════════════
# PROXY ROUTES
# ══════════════════════════════════════════════════════════════════════════

async def _proxy_request(method: str, path: str, body: dict | None = None,
                         headers: dict | None = None, stream: bool = False,
                         request: Request | None = None,
                         terminal_done: bool = True,
                         fault_accounting: bool = True,
                         dsml_suppress: bool = True) -> Response:
    """Generic proxy handler for OpenRouter API with multi-key retry loop.

    Iterates over all available keys on retriable failures (429, 5xx, network errors).
    Returns 429 (not 503) when all keys are exhausted so SDKs auto-retry.

    terminal_done: COMPATIBILITY_LAYER=2 (Anthropic upstream) streams end with
    message_stop, not [DONE]; pass terminal_done=False to skip the synthesized
    [DONE] terminator.

    fault_accounting: set False when the caller's own translator performs the
    B-39 mid-stream-fault error accounting (translated surfaces /v1/messages,
    /v1/responses) — otherwise one failed turn is counted twice.

    dsml_suppress: R5/double-scrub fix — set False when the caller's own
    translator performs DSML suppression + tool_use recovery itself
    (/v1/messages → _translate_openai_stream_to_anthropic); suppressing at
    the passthrough layer would strip the markup before it can be recovered.
    """
    model_id = (body or {}).get("model", "") if body else ""

    # Parity fix: openrouter was the ONLY wrapper with no call-plan validation
    # and no model-identity guard (MODEL_REGISTRY / same_provider_model_id were
    # imported but never used). Validate BEFORE spending an upstream request so
    # a mutated/invalid model id cannot burn a key's quota, and so the
    # "NO MODEL FALLBACK" contract is enforced here too.
    if model_id:
        surface = ('anthropic_messages' if '/messages' in path
                   else ('openai_responses' if '/responses' in path else 'openai_chat'))
        try:
            call_plan = MODEL_REGISTRY.call_plan(model_id, surface)
            if not same_provider_model_id('openrouter', call_plan.model.provider_model_id, model_id):
                return _error_response({"error": {
                    "type": "server_error",
                    "message": "Model identity changed during call-plan resolution",
                    "code": "MODEL_ID_MUTATION"}}, status_code=500)
        except ValueError as exc:
            return _error_response({"error": {
                "type": "invalid_request_error", "message": str(exc),
                "code": "MODEL_CALL_PLAN_INVALID"}}, status_code=400)
        except Exception as exc:  # registry unavailable — do not block traffic
            logger.debug(f'[openrouter] call-plan check skipped: {exc}')

    attempts = max(1, pool.total_keys)
    last_status = 429
    last_data = {"error": {"message": "All keys exhausted or rate-limited", "type": "rate_limit_error"}}

    for _ in range(attempts):
        acq = await pool.acquire(model=model_id)
        if not acq:
            break
        key_obj = acq['key']
        key_released = False  # I4: exactly-once release guard

        url = f"{OPENROUTER_BASE}/{path.lstrip('/')}"

        # Build headers: transparent proxy — forward ALL client headers via
        # shared build_forward_headers (broad allowlist), add upstream auth.
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
                    key_released = True  # I4: mark released, skip outer finally
                    # B-39 fix: record upstream stream failures.
                    _spawn_background(metrics.record_request(model=model_id, status_code=resp.status),
                                      'metrics-stream-err')
                    if model_id:
                        _spawn_background(_record_model_result(model_id, key_obj.api_key,
                                                               resp.status, last_data, url),
                                          'model-state')
                    last_status = resp.status
                    try:
                        last_data = _sanitize_nonfinite(json.loads(error_text))
                    except Exception:
                        last_data = {"error": {"message": error_text[:2000], "type": "upstream_error",
                                                 "status": resp.status}}
                    if _is_retriable_status(resp.status):
                        continue  # retry with next key
                    return JSONResponse(last_data, status_code=resp.status)
                pool.mark_success(key_obj, available_keys=pool.available_keys) if hasattr(pool, 'mark_success') else None
                # B-39 fix: streaming requests were never counted, so
                # error_rate was permanently ~0 and the dashboard reported
                # false health. Count the stream as a request now.
                _spawn_background(metrics.record_request(model=model_id, status_code=200),
                                  'metrics-stream')
                if model_id:
                    _spawn_background(_record_model_result(model_id, key_obj.api_key, 200, None, url),
                                      'model-state')

                # Heartbeat-aware streaming passthrough — keeps idle LBs/agents alive.
                # B-23: `heartbeat_ms` / `heartbeat_bytes` were dead locals —
                # the heartbeat interval and payload are handled inside
                # stream_with_heartbeat() below. Removed.
                resp_ref = resp
                released = False
                key_released = True  # I4: stream path owns release; outer finally skips

                async def stream_gen():
                    nonlocal released
                    # R-06: track whether upstream already terminated, and
                    # whether its last byte ended a line. Unconditionally
                    # appending b'data: [DONE]\n\n' produced the corrupt frame
                    # '[DONE]data: [DONE]' when the upstream sent [DONE]
                    # WITHOUT a trailing blank line — a real pattern, and the
                    # resulting line is not valid JSON, so strict SDK parsers
                    # error out at the very end of an otherwise good turn.
                    #
                    # Audit 2026-08-03: events are parsed + re-serialised via
                    # the SHARED PassthroughBlockRewriter (CONTRACT §7)
                    # (P0-4: scrub special tokens even on the raw passthrough —
                    # user report '<unk><unk>…'; P0-1: EOF without a terminal
                    # signal surfaces a shape-appropriate error frame per
                    # CONTRACT §3.3). dsml_suppress=False (call-site choice)
                    # leaves MiniMax DSML markup intact for the recovering
                    # /v1/messages translator (R5 double-scrub fix).
                    rw = _PassthroughBlockRewriter(dsml_suppress=dsml_suppress)
                    completed_naturally = False
                    cancelled = False

                    try:
                        async for line in resp_ref.content:
                            if not line:
                                continue
                            for fr_b in rw.feed(line):
                                yield fr_b
                        # COMPATIBILITY_LAYER=2: an Anthropic upstream ends its
                        # stream with message_stop, never [DONE] — appending
                        # [DONE] would corrupt the Anthropic SSE. Only
                        # synthesize [DONE] when terminal_done is requested.
                        for fr_b in rw.finish(terminal_done=terminal_done):
                            yield fr_b
                        completed_naturally = True
                    except (GeneratorExit, asyncio.CancelledError):
                        # Client went away — NOT an upstream fault (and never
                        # yield anything on this path, CONTRACT §3.5).
                        cancelled = True
                        raise
                    finally:
                        # B-39 parity (CONTRACT §10): count mid-stream faults —
                        # HTTP 200 was already committed. A client disconnect
                        # (cancelled) is NOT an upstream fault and must not
                        # inflate the error rate. fault_accounting=False means
                        # the caller's own translator counts it (exactly-once).
                        if fault_accounting and (getattr(rw, '_premature_emitted', False) or (
                                not completed_naturally and not cancelled)):
                            try:
                                metrics.record_error()
                            except Exception:
                                pass
                        if not released:
                            released = True
                            try:
                                resp_ref.release()
                            except Exception:
                                pass
                            pool.release(key_obj)

                async def stream_with_heartbeat():
                    # B-08 fix: use the shared sentinel-task iterator instead of
                    # asyncio.wait_for. wait_for CANCELS the pending read on
                    # timeout and its TimeoutError is indistinguishable from a
                    # genuine socket read timeout — so a DEAD upstream was
                    # heartbeated forever and the client hung until its own
                    # timeout. (nous N-05 / blackbox BB-5 parity.)
                    last_hb = time.time()
                    at_line_boundary = True
                    hb_interval = float(HEARTBEAT_MS) / 1000.0
                    inner = stream_gen()
                    try:
                        async for chunk in _iter_chunks_with_idle(inner, hb_interval):
                            now = time.time()
                            if chunk is _IDLE:
                                # Only inject a comment at a clean line boundary
                                # so a heartbeat can never split a data: frame.
                                if at_line_boundary and (now - last_hb) > hb_interval:
                                    yield b': heartbeat\n\n'
                                    last_hb = now
                                continue
                            yield chunk
                            if isinstance(chunk, (bytes, bytearray)) and len(chunk):
                                at_line_boundary = chunk.endswith(b'\n')
                            elif chunk:
                                at_line_boundary = str(chunk).endswith('\n')
                            last_hb = time.time()
                    except (GeneratorExit, asyncio.CancelledError):
                        raise
                    except Exception as e:
                        # B-08: a real upstream failure must terminate the
                        # stream visibly rather than being masked as idle.
                        logger.warning(
                            f'[openrouter] upstream stream error ({type(e).__name__}: {e}); finalizing')
                        yield f': upstream-error {type(e).__name__}\n\n'.encode()
                    finally:
                        try:
                            await inner.aclose()
                        except Exception:
                            pass

                return StreamingResponse(
                    stream_with_heartbeat(),
                    status_code=resp.status,
                    media_type="text/event-stream",
                    headers={
                        **{k: sanitize_header_value(v) for k, v in resp.headers.items()
                           if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')},
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            # Non-streaming
            async with agent.request(method, url, json=body, headers=fwd, timeout=timeout) as resp:
                await metrics.record_request(model=model_id, status_code=resp.status)
                text = await resp.text()
                if model_id:
                    _spawn_background(
                        _record_model_result(model_id, key_obj.api_key, resp.status,
                                             None if resp.status == 200 else text, url),
                        'model-state')
                if resp.status >= 400:
                    # Parse Retry-After header for 429 cooldown (anti rate-limit).
                    try:
                        body_data = _sanitize_nonfinite(json.loads(text)) if text else {}
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
                    data = _sanitize_nonfinite(json.loads(text)) if text else {}
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
            # I4: exactly-once release — skip if key already released
            # (stream path releases in stream_gen finally; stream-error path
            # releases inline before continue/return).
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
        headers={"Retry-After": retry_after},
    )


def _check_free_only(model: str) -> JSONResponse | None:
    """Check FREE_ONLY constraint. Returns error response if blocked.
    Returns 400 (not 403) for consistency with nous/opencode/blackbox wrappers."""
    if free_only_enabled() and model and not is_free_model(model):
        return _error_response(
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
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # WRAPPER_CONTRACT §4: max_tokens MUST be a positive integer capped at 1M.
    mt = body.get("max_tokens")
    if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0):
        return _error_response({"error": {"message": "max_tokens must be a positive integer",
                                          "type": "invalid_request_error"}}, status_code=400)
    if isinstance(mt, int) and mt > 1_000_000:
        return _error_response({"error": {"message": "max_tokens exceeds maximum allowed value of 1000000",
                                          "type": "invalid_request_error"}}, status_code=400)
    # WRAPPER_CONTRACT §4: unknown roles and orphan tool messages are rejected.
    for _m in body.get("messages") or []:
        if isinstance(_m, dict) and _m.get("role") not in (None, "system", "user", "assistant", "tool", "developer", "function"):
            return _error_response({"error": {"message": f"Invalid role: {_m.get('role')!r}",
                                              "type": "invalid_request_error"}}, status_code=400)
        if isinstance(_m, dict) and _m.get("role") == "tool" and not _m.get("tool_call_id"):
            return _error_response({"error": {"message": "tool role requires tool_call_id",
                                              "type": "invalid_request_error"}}, status_code=400)

    # FREE_ONLY check
    blocked = _check_free_only(model)
    if blocked:
        return blocked

    stream = body.get("stream", False)

    # COMPATIBILITY_LAYER=2: upstream speaks Anthropic Messages — translate the
    # OpenAI chat request once and translate the Anthropic response back.
    if await _upstream_is_anthropic():
        anthro_body = openai_chat_to_anthropic_request(body)
        anthro_body["stream"] = stream
        res = await _proxy_request("POST", "messages", anthro_body, stream=stream,
                                   request=request, terminal_done=False)
        if isinstance(res, StreamingResponse):
            return StreamingResponse(
                _translate_anthropic_stream_to_openai_chat(
                    res.body_iterator, model, float(HEARTBEAT_MS) / 1000.0),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": _hdr_echo(res.headers.get("x-request-id", "")),
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

    res = await _proxy_request("POST", "chat/completions", body, stream=stream, request=request)
    if isinstance(res, JSONResponse) and res.status_code == 200:
        try:
            payload = json.loads(res.body)
            if isinstance(payload, dict) and payload.get("type") == "message" and "content" in payload:
                oai_resp = anthropic_to_openai_response(payload, model)
                return JSONResponse(oai_resp, status_code=200)
            # P0-4: scrub special tokens from the non-stream reply body too.
            if isinstance(payload, dict) and "choices" in payload:
                _scrub_openai_response_inplace(payload)
                return JSONResponse(payload, status_code=200)
        except Exception:
            pass
    return res


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
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # WRAPPER_CONTRACT §4: max_output_tokens / max_tokens capped at 1M and
    # positive on the Responses surface too.
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

    # Translate Responses → Chat Completions
    principal = _request_principal(request)
    chat_body = responses_to_chat(body, principal=principal)
    is_stream = bool(chat_body.get("stream", False))

    # COMPATIBILITY_LAYER=2: Anthropic upstream — Responses → Chat → Anthropic
    # request; translate the Anthropic response back through OpenAI Chat to
    # Responses.
    if await _upstream_is_anthropic():
        anthro_body = openai_chat_to_anthropic_request(chat_body)
        response = await _proxy_request("POST", "messages", anthro_body, stream=is_stream,
                                        request=request, terminal_done=False)
        if isinstance(response, StreamingResponse):
            chat_sse = _translate_anthropic_stream_to_openai_chat(
                response.body_iterator, model, float(HEARTBEAT_MS) / 1000.0)
            return StreamingResponse(
                _translate_openai_chat_sse_to_responses(chat_sse, model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": _hdr_echo(response.headers.get("x-request-id", "")),
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
                rid = resp_obj.get('id')
                if rid and principal:
                    # R6 audit: store request messages + the assistant reply —
                    # replaying without the assistant tool_calls turn orphans
                    # the next function_call_output (upstream 400, §8 parity).
                    _store_response(principal, rid, chat_body.get('messages', []) + [_assistant_message_from_chat(oai_chat)])
                return JSONResponse(resp_obj, status_code=response.status_code)
        return response

    # fault_accounting=False: the Responses translator records mid-stream
    # faults itself — one failed turn must not be counted twice.
    response = await _proxy_request("POST", "chat/completions", chat_body, stream=is_stream, request=request,
                                    fault_accounting=False)

    if isinstance(response, JSONResponse):
        # R-05 (CRITICAL, same class): the raw OpenAI ChatCompletion body was
        # returned to Codex instead of a Responses object, because this branch
        # returned before the translation below could run.
        try:
            payload = json.loads(response.body)
        except Exception:
            return response
        if isinstance(payload, dict) and 'error' in payload:
            return JSONResponse(
                {"error": payload['error'], "type": "error"},
                status_code=response.status_code,
            )
        if isinstance(payload, dict) and 'choices' in payload:
            resp_obj = chat_to_responses(model, payload, body)
            rid = resp_obj.get('id')
            if rid and principal:
                # R6 audit: store request messages + the assistant reply
                # (orphan function_call_output on replay → upstream 400).
                _store_response(principal, rid, chat_body.get('messages', []) + [_assistant_message_from_chat(payload)])
            return JSONResponse(resp_obj, status_code=response.status_code)
        return response

    # Streaming: translate OpenAI SSE → Responses SSE event lifecycle.
    if is_stream and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_responses(
                response.body_iterator, model,
                store_ctx=(principal, list(chat_body.get('messages', [])))),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-request-id": _hdr_echo(response.headers.get("x-request-id", "")),
            },
        )

    # Non-streaming: translate Chat Completions JSON → Responses JSON.
    try:
        payload = json.loads(response.body)
        if isinstance(payload, dict) and 'choices' in payload:
            resp = chat_to_responses(model, payload, body)
            # Store for previous_response_id continuity.
            resp_id = resp.get('id')
            if resp_id and principal:
                # B-33: bounded + TTL-pruned write.
                # R6 audit: store request messages + the assistant reply —
                # without the assistant tool_calls turn, the next turn's
                # function_call_output replays as an orphan (upstream 400,
                # agent loop dies mid-run; nous/opencode/blackbox parity).
                _store_response(principal, resp_id, chat_body.get('messages', []) + [_assistant_message_from_chat(payload)])
            return JSONResponse(resp, status_code=response.status_code)
    except Exception as e:
        logger.warning(f"[openrouter] /v1/responses translation failed: {e}")
    return response


async def _translate_openai_stream_to_responses(openai_gen, model: str,
                                                store_ctx: "tuple | None" = None):
    """Translate OpenAI Chat Completions SSE stream → Responses API SSE stream.

    store_ctx: optional (principal, request_messages) — on a successful
    response.completed, the full turn (request messages + assistant reply
    incl. tool_calls) is written to the tenant store so a later
    previous_response_id turn replays an orphan-free history (R6 audit:
    streamed turns were never stored at all, so Codex streaming death-looped
    on the follow-up turn).

    Emits the full Responses event lifecycle:
      response.created → response.in_progress → response.output_item.added →
      response.content_part.added → response.output_text.delta →
      response.output_text.done → response.content_part.done →
      response.output_item.done → response.completed → [DONE]

    Reasoning and tool-call deltas are streamed as their own output items
    (`response.reasoning_text.delta` / `response.function_call_arguments.delta`) so
    Codex keeps receiving progress during the model's thinking phase and can
    act on structured tool calls.

    CODEX-RESP-01 (CRITICAL): the completion events MUST be emitted even when
    the model produced ONLY reasoning/thinking and no text content. The old
    text-gated guard skipped them, so Codex never saw its output items close
    and waited indefinitely for the terminal events — appearing to
    "stop mid-process". Reference: nous `ResponsesStreamState.done()`,
    opencode inline `gen()`.

    CRITICAL: each SSE event MUST be yielded as a SINGLE string with the
    `\n\n` terminator inline. Splitting `event:` and `data:` into separate
    yields causes Starlette to flush them as separate HTTP chunks; the client
    receives a partial frame and surfaces it as raw text.
    """
    resp_id = _new_response_id()  # R7: unique per turn (history-store safety)
    created_at = int(time.time())
    msg_id = _new_msg_id()  # R9: unique per stream
    full_text = ''
    upstream_error = None  # R-03: mid-stream upstream failure
    gen_fault = False  # B-39: transport-level fault surfaced via response.failed
    # Reasoning output item (CODEX-RESP-01): reasoning-only streams must still
    # open and close their output items or Codex hangs waiting for completion.
    reasoning_started = False
    acc_reason = ''
    rsn_index = 1
    rsn_id = f"rsn_{int(time.time()*1000)}"
    # Tool-call accumulation (parallel support), mirroring opencode/nous.
    tool_accs: list = []
    next_output_index = 1  # 0 = assistant message; 1+ = reasoning / tool items
    msg_open = False
    # P0-1: did the upstream send a finish_reason? EOF without one (and
    # without an error frame) is a premature close → response.failed.
    saw_finish: list = []
    acc_usage: list = []  # P1-3: last upstream usage (canonical details shape)
    # P0-4: cross-chunk special-token scrubbers (one per visible channel).
    _tok_text = _SpecialTokenFilter()
    _tok_reason = _SpecialTokenFilter()
    # R5 audit: DSML markup suppression on the visible text channel.
    _dsml_text = _DsmlMarkupFilter()

    def _sse(event_type: str, payload: dict) -> str:
        """Build a complete SSE frame: event:\ndata:\n\n"""
        return f'event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'

    try:
        # response.created
        yield _sse('response.created', {
            'type': 'response.created',
            'response': {
                'id': resp_id, 'object': 'response', 'created_at': created_at,
                'model': model, 'status': 'in_progress', 'output': [],
                # P1-3: full usage shape (details structures) for strict SDKs.
                'usage': _responses_usage(),
            },
        })

        # response.in_progress
        yield _sse('response.in_progress', {
            'type': 'response.in_progress',
            'response': {'id': resp_id, 'status': 'in_progress'},
        })

        # CODEX-RESP-01: open the assistant message item EAGERLY so even a
        # reasoning-only stream has an active item. OpenAI Responses requires
        # the output item to be "added" before any delta is sent, and Codex
        # emits "OutputTextDelta without active item" and hangs otherwise.
        yield _sse('response.output_item.added', {
            'type': 'response.output_item.added', 'output_index': 0,
            'item': {
                'id': msg_id, 'type': 'message', 'status': 'in_progress',
                'role': 'assistant', 'content': [],
            },
        })
        yield _sse('response.content_part.added', {
            'type': 'response.content_part.added', 'item_id': msg_id,
            'output_index': 0, 'content_index': 0,
            'part': {'type': 'output_text', 'text': '', 'annotations': []},
        })
        msg_open = True

        # Buffer accumulator: upstream chunks may contain multiple SSE lines
        # or partial lines split across chunk boundaries. Accumulate and split
        # on \n to parse complete lines only.
        buffer = b''
        hb_interval = float(HEARTBEAT_MS) / 1000.0
        done = False

        def _get_tool_acc(tc: dict) -> dict:
            nonlocal next_output_index
            idx = tc.get('index') if isinstance(tc.get('index'), int) else len(tool_accs)
            acc = tool_accs[idx] if idx < len(tool_accs) else None
            if acc is None:
                acc = {'call_id': tc.get('id') or f'call_{idx}_{int(time.time()*1000)}',
                       'name': '', 'args': '', 'output_index': next_output_index, 'added': False}
                next_output_index += 1
                while len(tool_accs) <= idx:
                    tool_accs.append(None)
                tool_accs[idx] = acc
            if tc.get('id'):
                acc['call_id'] = tc['id']
            return acc

        def _parse_line(line_str: str):
            """Parse one complete SSE line. Returns (events_list, is_done)."""
            nonlocal full_text, upstream_error, reasoning_started, acc_reason, rsn_index, next_output_index
            events = []
            # B-02 fix: the space after `data:` is OPTIONAL in SSE. Requiring
            # it silently discarded 100% of chunks from upstreams that emit
            # `data:{...}`, producing an empty answer with no log or error.
            if not line_str.startswith('data:'):
                return events, False
            data_str = line_str[5:].strip()
            if data_str == '[DONE]':
                return events, True
            try:
                data = json.loads(data_str)
            except (json.JSONDecodeError, RecursionError):
                return events, False
            # R-03: surface a mid-stream upstream error instead of dropping it
            # and closing with a fabricated response.completed.
            if isinstance(data, dict) and data.get('error') is not None and 'choices' not in data:
                _e = data['error']
                upstream_error = (_e.get('message') if isinstance(_e, dict) else str(_e)) or 'upstream error'
                logger.error(f'[openrouter responses] upstream error frame: {upstream_error}')
                return events, True
            if data.get('usage'):
                # P1-3: canonical full-details usage for response.completed.
                acc_usage.clear()
                acc_usage.append(_responses_usage(*_tokens_from_chat_usage(data['usage'])))
            if data.get('object') != 'chat.completion.chunk':
                return events, False
            choices = data.get('choices', [])
            if not choices:
                return events, False
            choice = choices[0]
            delta = choice.get('delta', {}) or {}

            # Text content. F2/parity: `content` is str for the OpenAI shape,
            # but some upstreams stream multi-part content arrays — guard the
            # type so a list doesn't raise TypeError mid-stream (HTTP 500
            # kills the turn), mirroring nvidia-python/nous.
            content = delta.get('content')
            if isinstance(content, str):
                if content:
                    # R5: suppress DSML markup first (cross-chunk), then P0-4
                    # scrub special tokens (cross-chunk).
                    content = _tok_text.feed(_dsml_text.feed(content))
                if content:
                    full_text += content
                    events.append(_sse('response.output_text.delta', {
                        'type': 'response.output_text.delta', 'item_id': msg_id,
                        'output_index': 0, 'content_index': 0, 'delta': content,
                    }))
            elif isinstance(content, list):
                parts = [p.get('text') for p in content
                         if isinstance(p, dict) and isinstance(p.get('text'), str) and p.get('text')]
                if parts:
                    joined = _tok_text.feed(_dsml_text.feed(''.join(parts)))
                    if joined:
                        full_text += joined
                        events.append(_sse('response.output_text.delta', {
                            'type': 'response.output_text.delta', 'item_id': msg_id,
                            'output_index': 0, 'content_index': 0, 'delta': joined,
                        }))

            # Reasoning (OpenRouter reasoning_content / reasoning) — MUST be
            # streamed so the client sees progress during thinking (Codex /
            # OpenAI SDK abort a silent stream → "stops mid-way"). Mirror
            # nous/opencode: open a 'reasoning' output item, then deltas.
            reason_delta = (
                delta.get('reasoning_content') if isinstance(delta.get('reasoning_content'), str)
                else (delta.get('reasoning') if isinstance(delta.get('reasoning'), str) else '')
            )
            if reason_delta:
                # P0-4: scrub special tokens from the reasoning channel.
                reason_delta = _tok_reason.feed(reason_delta)
            if reason_delta:
                if not reasoning_started:
                    reasoning_started = True
                    rsn_index = next_output_index
                    next_output_index += 1
                    events.append(_sse('response.output_item.added', {
                        'type': 'response.output_item.added', 'output_index': rsn_index,
                        'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'in_progress',
                                 'summary': [], 'content': []},
                    }))
                acc_reason += reason_delta
                events.append(_sse('response.reasoning_text.delta', {
                    'type': 'response.reasoning_text.delta', 'item_id': rsn_id,
                    'output_index': rsn_index, 'content_index': 0, 'delta': reason_delta,
                }))

            # Tool calls (parallel support, mirroring opencode/nous): every
            # added function_call item MUST be closed with output_item.done or
            # Codex hangs waiting for a tool result.
            for tc in delta.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get('function') or {}
                acc = _get_tool_acc(tc)
                if not acc['added']:
                    acc['added'] = True
                    events.append(_sse('response.output_item.added', {
                        'type': 'response.output_item.added', 'output_index': acc['output_index'],
                        'item': {
                            'id': acc['call_id'], 'type': 'function_call', 'status': 'in_progress',
                            'call_id': acc['call_id'], 'name': acc['name'], 'arguments': '',
                        },
                    }))
                if isinstance(fn.get('name'), str) and fn['name']:
                    # P0-3 fix: never emit the NAME as an arguments delta —
                    # delta-accumulating clients collected `name{...}` (invalid
                    # JSON). The name is carried by output_item.added.item and
                    # by function_call_arguments.done ('name' field) instead.
                    acc['name'] += fn['name']
                if isinstance(fn.get('arguments'), str) and fn['arguments']:
                    acc['args'] += fn['arguments']
                    events.append(_sse('response.function_call_arguments.delta', {
                        'type': 'response.function_call_arguments.delta', 'item_id': acc['call_id'],
                        'output_index': acc['output_index'], 'delta': fn['arguments'],
                    }))

            if choice.get('finish_reason'):
                saw_finish.append(True)
                return events, True
            return events, False

        # B-08 fix: sentinel-task idle detection (see common/sse.py). The old
        # asyncio.wait_for could not distinguish an idle upstream from a dead
        # one, so a failed stream was heartbeated indefinitely.
        async for raw in _iter_chunks_with_idle(openai_gen, hb_interval):
            if done:
                break
            if raw is _IDLE:
                yield ': heartbeat\n\n'
                continue
            if isinstance(raw, str):
                raw = raw.encode('utf-8', errors='replace')
            buffer += raw
            # Parity fix: tolerate CRLF SSE framing (nous N-08).
            buffer = _normalize_sse_newlines(buffer)
            # Split on \n, process complete lines, retain tail in buffer
            while b'\n' in buffer:
                line_bytes, buffer = buffer.split(b'\n', 1)
                line_str = line_bytes.decode('utf-8', errors='replace').strip()
                if not line_str:
                    continue
                events, is_done = _parse_line(line_str)
                for ev in events:
                    yield ev
                if is_done:
                    done = True
                    break

        # CODEX-RESP-01 fix: completion events are emitted UNCONDITIONALLY.
        # The old text-gated guard skipped them when the model emitted only
        # reasoning/thinking, so Codex never saw output items close and hung
        # waiting for the terminal events.
        # P0-1 (CONTRACT §3.3): the upstream stream ended WITHOUT any terminal
        # signal (no finish_reason, no error frame). Completing now would
        # persist a truncated answer as a successful turn — fail visibly.
        if upstream_error is None and not saw_finish:
            upstream_error = ('upstream stream ended prematurely: EOF without '
                              'finish_reason or [DONE]; the response may be '
                              'truncated — client may retry')
            logger.error('[openrouter responses] upstream stream ended prematurely (no finish_reason)')
        # P0-4/R5: release filter-withheld tail text (DSML remnant still
        # passes the token scrubber) before the done events.
        _rest_text = _tok_text.feed(_dsml_text.flush()) + _tok_text.flush()
        if _rest_text:
            full_text += _rest_text
            yield _sse('response.output_text.delta', {
                'type': 'response.output_text.delta', 'item_id': msg_id,
                'output_index': 0, 'content_index': 0, 'delta': _rest_text,
            })
        if reasoning_started:
            _rest_rsn = _tok_reason.flush()
            if _rest_rsn:
                acc_reason += _rest_rsn
                yield _sse('response.reasoning_text.delta', {
                    'type': 'response.reasoning_text.delta', 'item_id': rsn_id,
                    'output_index': rsn_index, 'content_index': 0, 'delta': _rest_rsn,
                })
            yield _sse('response.reasoning_text.done', {
                'type': 'response.reasoning_text.done', 'item_id': rsn_id,
                'output_index': rsn_index, 'content_index': 0, 'text': acc_reason,
            })
            yield _sse('response.output_item.done', {
                'type': 'response.output_item.done', 'output_index': rsn_index,
                'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'completed',
                         'summary': [],
                         'content': [{'type': 'reasoning_text', 'text': acc_reason}]},
            })
        # output_text.done
        yield _sse('response.output_text.done', {
            'type': 'response.output_text.done', 'item_id': msg_id,
            'output_index': 0, 'content_index': 0, 'text': full_text,
        })
        # content_part.done
        yield _sse('response.content_part.done', {
            'type': 'response.content_part.done', 'item_id': msg_id,
            'output_index': 0, 'content_index': 0,
            'part': {'type': 'output_text', 'text': full_text, 'annotations': []},
        })
        # output_item.done
        yield _sse('response.output_item.done', {
            'type': 'response.output_item.done', 'output_index': 0,
            'item': {
                'id': msg_id, 'type': 'message', 'status': 'completed',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}],
            },
        })
        # Close every function_call item that was opened (Codex hangs if a
        # function_call item is added but never marked done).
        # CODEX-RESP-02: emit the standard `response.function_call_arguments.done`
        # before closing the item so the SDK finalizes the parsed arguments.
        for acc in tool_accs:
            if not acc:
                continue
            yield _sse('response.function_call_arguments.done', {
                'type': 'response.function_call_arguments.done', 'item_id': acc['call_id'],
                'output_index': acc['output_index'], 'name': acc['name'], 'arguments': acc['args'],
            })
            yield _sse('response.output_item.done', {
                'type': 'response.output_item.done', 'output_index': acc['output_index'],
                'item': {
                    'id': acc['call_id'], 'type': 'function_call', 'status': 'completed',
                    'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args'],
                },
            })

        # R-03: report failure rather than fabricating a successful completion.
        if upstream_error:
            yield _sse('response.failed', {
                'type': 'response.failed',
                'response': {
                    'id': resp_id, 'object': 'response', 'created_at': created_at,
                    'model': model, 'status': 'failed',
                    'error': {'code': 'upstream_error',
                              'message': str(upstream_error)[:2000]},
                },
            })
            yield 'data: [DONE]\n\n'
            return

        # response.completed — final output array sorted by output_index
        # (0 = message, then reasoning / tool items in open order).
        outputs_by_index = {
            0: {'id': msg_id, 'type': 'message', 'status': 'completed',
                'role': 'assistant',
                'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}]},
        }
        if reasoning_started:
            outputs_by_index[rsn_index] = {'id': rsn_id, 'type': 'reasoning',
                                           'status': 'completed', 'summary': [], 'content': [{'type': 'reasoning_text', 'text': acc_reason}]}
        for acc in tool_accs:
            if not acc:
                continue
            outputs_by_index[acc['output_index']] = {
                'id': acc['call_id'], 'type': 'function_call', 'status': 'completed',
                'call_id': acc['call_id'], 'name': acc['name'], 'arguments': acc['args'],
            }
        output = [outputs_by_index[i] for i in sorted(outputs_by_index)]
        yield _sse('response.completed', {
            'type': 'response.completed',
            'response': {
                'id': resp_id, 'object': 'response', 'created_at': created_at,
                'model': model, 'status': 'completed', 'output': output,
                'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [],
                # P1-3: full usage (details structures) — strict SDKs require it.
                'usage': acc_usage[0] if acc_usage else _responses_usage(),
            },
        })

        # R6 audit: persist the completed streamed turn (request + assistant
        # reply incl. tool_calls) — previous_response_id replay must see an
        # orphan-free history. Never on failure/disconnect paths.
        if store_ctx is not None:
            try:
                _principal, _req_msgs = store_ctx
                _store_response(
                    _principal, resp_id,
                    list(_req_msgs) + [_assistant_message_from_chat(
                        {}, full_text, [a for a in tool_accs if a])])
            except Exception as _se:
                logger.warning(f'[openrouter] responses store write failed: {_se}')

        yield 'data: [DONE]\n\n'

    except (GeneratorExit, asyncio.CancelledError):
        # B-09 parity: never yield during async-generator finalization.
        raise
    except Exception as e:
        logger.error(f'[openrouter] responses stream translation error: {e}')
        # B-07 fix: a transport failure must surface as response.failed, NOT as
        # a fabricated response.completed carrying partial text — the client
        # otherwise persists a truncated answer as a successful turn and
        # cannot retry. (blackbox B20 parity.)
        gen_fault = True
        try:
            if msg_open:
                if reasoning_started:
                    yield _sse('response.reasoning_text.done', {
                        'type': 'response.reasoning_text.done', 'item_id': rsn_id,
                        'output_index': rsn_index, 'content_index': 0, 'text': acc_reason,
                    })
                    yield _sse('response.output_item.done', {
                        'type': 'response.output_item.done', 'output_index': rsn_index,
                        'item': {'id': rsn_id, 'type': 'reasoning', 'status': 'completed',
                                 'summary': [],
                                 'content': [{'type': 'reasoning_text', 'text': acc_reason}]},
                    })
                yield _sse('response.output_text.done', {
                    'type': 'response.output_text.done', 'item_id': msg_id,
                    'output_index': 0, 'content_index': 0, 'text': full_text,
                })
                yield _sse('response.content_part.done', {
                    'type': 'response.content_part.done', 'item_id': msg_id,
                    'output_index': 0, 'content_index': 0,
                    'part': {'type': 'output_text', 'text': full_text, 'annotations': []},
                })
                yield _sse('response.output_item.done', {
                    'type': 'response.output_item.done', 'output_index': 0,
                    'item': {
                        'id': msg_id, 'type': 'message', 'status': 'completed',
                        'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': full_text, 'annotations': []}],
                    },
                })
            # B-07 fix: report FAILURE, not a fabricated success.
            yield _sse('response.failed', {
                'type': 'response.failed',
                'response': {
                    'id': resp_id, 'object': 'response', 'created_at': created_at,
                    'model': model, 'status': 'failed',
                    'error': {'code': 'upstream_error',
                              'message': f'upstream stream error: {str(e)[:2000]}'},
                },
            })
            yield 'data: [DONE]\n\n'
        except Exception:
            pass
    finally:
        # B-39 parity (CONTRACT §10): count mid-stream faults — response.failed
        # after a committed HTTP 200 is invisible to per-status accounting.
        if gen_fault or upstream_error is not None:
            try:
                metrics.record_error()
            except Exception:
                pass
        # B-09 fix: deterministically release the upstream response + pool key.
        try:
            aclose = getattr(openai_gen, 'aclose', None)
            if aclose is not None:
                await aclose()
        except Exception:
            pass
# ══════════════════════════════════════════════════════════════════════════
# EMBEDDINGS / IMAGES / MODELS ROUTES
# ══════════════════════════════════════════════════════════════════════════
@app.post("/v1/embeddings")
async def embeddings(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return await _proxy_request("POST", "embeddings", body, request=request)


@app.post("/v1/images/generations")
async def images_generations(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
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
        # B-37.1: release exactly once on EVERY path. Previously a raised
        # agent.get (connect error/timeout/DNS) skipped pool.release and
        # stranded the key's in-flight slot until heal_in_flight's ~600s
        # threshold — repeated /v1/models pulls against a down upstream could
        # exhaust the whole pool (agents poll discovery at startup).
        try:
            async with agent.get(f"{OPENROUTER_BASE}/models", headers=headers) as resp:
                pool.release(key_obj)
                key_obj = None
                if resp.status != 200:
                    return JSONResponse({"data": []}, status_code=resp.status)
                data = _sanitize_nonfinite(await resp.json())
                models = data.get("data", [])

                # Filter FREE_ONLY
                if free_only_enabled():
                    models = [m for m in models if is_free_model(m.get("id", ""))]

                return JSONResponse({"data": models, "object": "list"})
        finally:
            if key_obj is not None:
                pool.release(key_obj)
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


@app.get("/v1/capabilities")
async def capabilities(request: Request):
    """Model capability discovery.

    WRAPPER_CONTRACT v3.0 §2.1 requires this surface on every wrapper; it was
    the only one of the mandated endpoints missing from openrouter (gap found
    while verifying the contract against the code on 2026-08-01). Shape matches
    the nous/opencode/blackbox implementations so a client can treat all five
    wrappers identically.
    """
    models_list = []
    try:
        resp = await list_models(request)
        payload = json.loads(resp.body) if hasattr(resp, 'body') else {}
        models_list = payload.get('data', []) if isinstance(payload, dict) else []
    except Exception as e:
        logger.warning(f'[capabilities] model list unavailable: {e}')
        models_list = []
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
        "summary": {"total": len(models_list),
                    "by_type": {"chat": len(models_list)}},
        "dynamic_alias_target": None,
    }


# ── Anthropic-compatible ─────────────────────────────────────────────────

@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic Messages API → OpenAI Chat Completions translation."""
    try:
        body = await request.json()
    except Exception:
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    model = body.get("model", "")

    # P3 fix (fleet parity, audit 2026-08-03): the other four wrappers
    # REQUIRE max_tokens on /v1/messages (Anthropic contract) — a request
    # without it got 200 here while nous/opencode/blackbox/nvidia answered
    # 400, so behaviour differed per backend and unbounded generations could
    # blow through cost limits. Validate like the fleet: positive int ≤ 1e6.
    _mt = body.get("max_tokens")
    if not isinstance(_mt, int) or _mt <= 0:
        return _error_response({"error": {"message": "max_tokens is required and must be a positive integer",
                                          "type": "invalid_request_error"}}, status_code=400)
    if _mt > 1000000:
        return _error_response({"error": {"message": "max_tokens exceeds maximum allowed value of 1000000",
                                          "type": "invalid_request_error"}}, status_code=400)

    # WRAPPER_CONTRACT §4: unknown roles / orphan tool messages are rejected.
    for _m in body.get("messages") or []:
        if isinstance(_m, dict) and _m.get("role") not in (None, "user", "assistant", "tool", "system", "developer"):
            return _error_response({"error": {"message": f"Invalid role: {_m.get('role')!r}",
                                              "type": "invalid_request_error"}}, status_code=400)
        if isinstance(_m, dict) and _m.get("role") == "tool" and not _m.get("tool_use_id") and not _m.get("tool_call_id"):
            return _error_response({"error": {"message": "tool message requires tool_use_id",
                                              "type": "invalid_request_error"}}, status_code=400)

    blocked = _check_free_only(model)
    if blocked:
        return blocked

    # COMPATIBILITY_LAYER=2: upstream speaks Anthropic — the /v1/messages
    # surface passes through verbatim (model alias only); no translation.
    if await _upstream_is_anthropic():
        response = await _proxy_request("POST", "messages", body,
                                        stream=bool(body.get("stream", False)),
                                        request=request, terminal_done=False)
        if isinstance(response, StreamingResponse):
            return StreamingResponse(
                _passthrough_anthropic_sse(response.body_iterator,
                                           float(HEARTBEAT_MS) / 1000.0),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache", "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "x-request-id": _hdr_echo(response.headers.get("x-request-id", "")),
                },
            )
        return response

    # Translate Anthropic → OpenAI
    openai_body = _anthropic_to_openai(body)
    openai_body["stream"] = body.get("stream", False)

    # fault_accounting=False: the OpenAI→Anthropic translator below records
    # mid-stream faults itself — one failed turn must not be counted twice.
    # dsml_suppress=False: that translator does its own DSML suppression +
    # tool_use recovery — stripping the markup here first would silently
    # lose the tool call (R5 double-scrub finding, runtime e2e dsml_stream).
    response = await _proxy_request("POST", "chat/completions", openai_body,
                                     stream=openai_body["stream"], request=request,
                                     fault_accounting=False, dsml_suppress=False)

    if isinstance(response, JSONResponse):
        # RUNTIME FINDING R-05 (CRITICAL): this branch returned the RAW OpenAI
        # ChatCompletion body on success — `return response` fired before the
        # Anthropic translation further down could ever run, because
        # _proxy_request always returns a JSONResponse for non-streaming calls.
        # Claude Code received {"object":"chat.completion","choices":[...]}
        # instead of {"type":"message","content":[...]} and could not parse the
        # reply at all. Translate here, where the response actually arrives.
        try:
            payload = json.loads(response.body)
        except Exception:
            return response
        if isinstance(payload, dict) and 'error' in payload:
            return JSONResponse(
                {"type": "error", "error": payload['error']},
                status_code=response.status_code,
            )
        if isinstance(payload, dict) and 'choices' in payload:
            return JSONResponse(_openai_to_anthropic_response(payload, body),
                                status_code=response.status_code)
        return response

    # For streaming, translate OpenAI SSE → Anthropic SSE
    if openai_body["stream"] and isinstance(response, StreamingResponse):
        return StreamingResponse(
            _translate_openai_stream_to_anthropic(response.body_iterator, body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-request-id": _hdr_echo(response.headers.get("x-request-id", "")),
            },
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
        return _error_response({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    if not isinstance(body, dict):
        return _error_response({"error": {"message": "Request body must be a JSON object",
                                          "type": "invalid_request_error"}}, status_code=400)
    # Best-effort: count characters / 4 as a rough token estimate without burning quota.
    # Round-4 audit: iterate defensively — the JSONBodyGuard only inspects
    # inference surfaces, and a malformed nested shape (non-dict messages,
    # string content blocks) used to detonate an AttributeError here = HTTP 500.
    msgs = body.get("messages") or []
    total_chars = 0
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            c = m.get("content", "")
            if isinstance(c, str):
                total_chars += len(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict):
                        total_chars += len(b.get("text") or "")
                        inp = b.get("input")
                        if inp is not None:
                            total_chars += len(json.dumps(inp, ensure_ascii=False))
    est_tokens = max(1, total_chars // 4)
    return JSONResponse({"input_tokens": est_tokens})


# ── Responses API (Codex/Claude Code) ↔ Chat Completions translation ──────
# These translators allow the wrapper to accept /v1/responses requests
# (which use `input` instead of `messages`) and forward them as standard
# /v1/chat/completions to OpenRouter — then translate the response back.

# Tenant-isolated response store for previous_response_id continuity.
# B-33 fix: this store was COMPLETELY UNBOUNDED — one full conversation
# history per /v1/responses call, retained for the process lifetime, with no
# eviction anywhere. A long-running Codex session leaked until OOM. Now bounded
# by entry count, total bytes, and TTL (opencode/nous parity).
_RESPONSE_STORE: "OrderedDict[str, tuple]" = OrderedDict()
_RESPONSE_STORE_MAX_ENTRIES = int(os.environ.get('RESPONSES_STORE_MAX_ENTRIES', '200'))
_RESPONSE_STORE_TTL_SEC = int(os.environ.get('RESPONSES_STORE_TTL_SEC', '3600'))
_RESPONSE_STORE_MAX_BYTES = int(os.environ.get('RESPONSES_STORE_MAX_BYTES', str(32 * 1024 * 1024)))



def _new_msg_id() -> str:
    """R9: unique Anthropic/message-item id (bare ms timestamps collided across
    concurrent turns; CSPRNG suffix closes the window — R7-class)."""
    return f"msg_{int(time.time()*1000)}-{secrets.token_hex(4)}"


def _store_response(principal: str, response_id: str, messages: list) -> None:
    """Bounded, TTL-pruned write to the response store (B-33)."""
    if not response_id or not principal:
        return
    key = _response_store_key(principal, response_id)
    try:
        size = len(json.dumps(messages, ensure_ascii=False))
    except (TypeError, ValueError):
        size = 0
    # Reject a single oversized entry outright rather than evicting everything.
    if size > _RESPONSE_STORE_MAX_BYTES:
        logger.warning('[responses] history for %s too large (%dB); not stored', response_id, size)
        return
    # N-19 parity (nous): store a DEEP COPY so any later in-place mutation of
    # the live request/assistant message dicts (normalisation, sanitisation,
    # concurrent replays sharing the same original dicts) can never corrupt
    # the stored replay history.
    try:
        messages = copy.deepcopy(messages)
    except (TypeError, ValueError, RecursionError):
        messages = list(messages)
    _RESPONSE_STORE[key] = (time.time(), size, messages)
    _RESPONSE_STORE.move_to_end(key)
    _prune_response_store()


def _prune_response_store() -> None:
    """Evict expired, then oldest, entries until within all bounds (B-33)."""
    now = time.time()
    if _RESPONSE_STORE_TTL_SEC > 0:
        for k in [k for k, (ts, _s, _m) in _RESPONSE_STORE.items()
                  if now - ts > _RESPONSE_STORE_TTL_SEC]:
            _RESPONSE_STORE.pop(k, None)
    while len(_RESPONSE_STORE) > _RESPONSE_STORE_MAX_ENTRIES:
        _RESPONSE_STORE.popitem(last=False)
    total = sum(s for _ts, s, _m in _RESPONSE_STORE.values())
    while total > _RESPONSE_STORE_MAX_BYTES and len(_RESPONSE_STORE) > 1:
        _k, (_ts, s, _m) = _RESPONSE_STORE.popitem(last=False)
        total -= s


def _get_stored_conversation(principal: str, response_id: str) -> list:
    """Read a stored history, honouring TTL (B-33)."""
    key = _response_store_key(principal, response_id)
    entry = _RESPONSE_STORE.get(key)
    if not entry:
        return []
    ts, _size, msgs = entry
    if _RESPONSE_STORE_TTL_SEC > 0 and (time.time() - ts) > _RESPONSE_STORE_TTL_SEC:
        _RESPONSE_STORE.pop(key, None)
        return []
    # N-19 parity: return a DEEP COPY — the caller replays these dicts into a
    # live request body; in-place edits on the replay must not poison the
    # stored entry (or concurrent replays of the same response id).
    try:
        return copy.deepcopy(msgs)
    except (TypeError, ValueError, RecursionError):
        return list(msgs)


def _assistant_message_from_chat(data: dict, fallback_text: str = '', tool_accs=None) -> dict:
    """Persistable assistant chat message for the response store
    (opencode/blackbox/nous/nvidia parity — §8).

    R6 audit: openrouter stored ONLY the request messages — a later
    previous_response_id turn replayed a history whose assistant tool_calls
    turn was missing, so the client's function_call_output became an orphan
    role:tool upstream (400, agent loop dies mid-run). The stored history is
    the request messages + THIS assistant reply (incl. tool_calls)."""
    msg = (data.get('choices') or [{}])[0].get('message', {}) if isinstance(data, dict) else {}
    content = msg.get('content')
    if content is None:
        content = fallback_text if fallback_text is not None else None
    tool_calls = msg.get('tool_calls') or []
    if tool_accs:
        tool_calls = [
            {'id': acc.get('call_id'), 'type': 'function',
             'function': {'name': acc.get('name', ''), 'arguments': acc.get('args', '')}}
            for acc in tool_accs if acc
        ]
    out = {'role': 'assistant', 'content': content if content not in ('', None) else (None if tool_calls else '')}
    if tool_calls:
        out['tool_calls'] = tool_calls
    return out


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
            if t == 'reasoning':
                continue  # multi-turn Codex input includes reasoning items; chat has no placeholder
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
                # P1-1 fix: input_image parts were silently dropped — the
                # shared helper converts them to OpenAI image_url parts.
                c = _responses_content_to_chat(c)
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
    # P0-4: scrub special tokens from non-stream visible channels too.
    # R5 audit: strip DSML tool markup from the visible output text too.
    text = _strip_dsml_markup(_filter_special_tokens(msg.get('content') or ''))
    output = []
    # Surface upstream reasoning_content as a reasoning output item.
    # CODEX-RESP-02: the SDK's ResponseReasoningItem expects summary/content
    # as lists — `text` alone parses with serializer warnings.
    reasoning = _filter_special_tokens(msg.get('reasoning_content') or msg.get('reasoning') or '')
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
    output.append({'id': _new_msg_id(), 'type': 'message',
                   'status': 'completed', 'role': 'assistant',
                   'content': [{'type': 'output_text', 'text': text, 'annotations': []}]})
    # P1-3 fix: the Responses usage object requires the *_details structures.
    _in, _out, _cached, _rsn = _tokens_from_chat_usage(data.get('usage'))
    # CODEX-RESP-02: the openai SDK's Response model REQUIRES top-level
    # parallel_tool_calls / tool_choice / tools — missing them fails
    # non-streaming client.responses.create() parsing.
    # R7 concurrency: ALWAYS mint a fresh unique id — reusing the upstream
    # chat completion id (or an ms timestamp) collides across concurrent
    # turns and agents then replayed each other's stored history.
    resp = {'id': _new_response_id(),
            'object': 'response', 'created_at': int(time.time()),
            'model': model, 'status': 'completed', 'output': output,
            'parallel_tool_calls': True, 'tool_choice': 'auto', 'tools': [],
            'usage': _responses_usage(_in, _out, _cached, _rsn)}
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
                reasoning_parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text", ""))
                    elif btype in ("thinking", "reasoning"):
                        # Transparent passthrough: keep multi-turn reasoning
                        # context (parity with nous/opencode/blackbox, which
                        # map thinking blocks to reasoning_content).
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
                        else:
                            url = src.get("url", "")
                            if url:
                                image_parts.append({
                                    "type": "image_url",
                                    "image_url": {"url": url},
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
            openai_body[dst] = body[src]
    # Strip Anthropic-only cache_control annotations (upstream may 400 on them).
    _strip_cache(openai_body)
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
    if isinstance(openai_resp, dict) and openai_resp.get("type") == "message" and "content" in openai_resp:
        return openai_resp
    choices = openai_resp.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {})
    content_blocks = []
    # Transparent passthrough: surface reasoning_content as a thinking block
    # (parity with nous/opencode/blackbox/shared translators). Dropping it
    # silently removes part of the model's output.
    # P0-4: scrub special tokens from visible channels.
    reasoning = _filter_special_tokens(message.get("reasoning_content") or message.get("reasoning") or "")
    if reasoning:
        # P1-2: thinking blocks require a `signature` field (strict SDK parse).
        content_blocks.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    _msg_raw = message.get("content")
    _dsml_tools: list = []
    if isinstance(_msg_raw, str) and 'DSML' in _msg_raw.replace('\uff5c', '|'):
        # R5 (non-stream parity with nous/opencode/blackbox/nvidia): recover
        # MiniMax DSML tool markup leaking through the reply as real
        # tool_use blocks — never forward the raw markup to the client.
        try:
            from common.translations import parse_dsml_from_text as _pdsml
            _msg_raw, _dsml_tools = _pdsml(_msg_raw)
        except Exception:
            _dsml_tools = []
    _msg_text = _filter_special_tokens(_msg_raw or "") if isinstance(_msg_raw, str) else _msg_raw
    if _msg_text:
        content_blocks.append({"type": "text", "text": _msg_text})
    for _tu in _dsml_tools:
        if not isinstance(_tu, dict):
            continue
        content_blocks.append({
            "type": "tool_use",
            "id": _tu.get("id") or f"toolu_dsml_{len(content_blocks)}-{secrets.token_hex(3)}",
            "name": _tu.get("name") or "",
            "input": _tu.get("input") if isinstance(_tu.get("input"), dict) else {},
        })
    if message.get("tool_calls"):
        for tc in message["tool_calls"]:
            fn = tc.get("function", {})
            try:
                input_obj = _sanitize_nonfinite(json.loads(fn.get("arguments", "{}")))
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
    # R5: DSML-recovered tool_use blocks — MiniMax reports finish 'stop';
    # upgrade so the client executes the tool instead of ending the turn.
    if _dsml_tools and stop_reason == "end_turn":
        stop_reason = "tool_use"

    usage = openai_resp.get("usage", {})
    return {
        "id": openai_resp.get("id", _new_msg_id()),
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

    CRITICAL: each SSE event MUST be yielded as a SINGLE string with the
    `\n\n` terminator inline. Splitting `event:` and `data:` into separate
    yields causes Starlette to flush them as separate HTTP chunks; the client
    receives a partial frame (`event: ...\n` with no data and no blank-line
    terminator) and surfaces it as raw text (Claude Code bug).
    """
    msg_id = _new_msg_id()  # R9: unique per stream (ms alone collides across concurrent turns)
    model = request_body.get("model", "")

    def _sse(event_type: str, payload: dict) -> str:
        """Build a complete SSE frame: event:\ndata:\n\n"""
        return f'event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'

    block_open = False
    block_index = 0
    text_started = False
    thinking_started = False
    thinking_index = None
    tool_call_blocks = {}  # OpenAI tool index → Anthropic block index
    open_tool_blocks: set = set()  # R-02: concurrently-open tool_use blocks
    finish_reason = None
    output_tokens = 0
    input_tokens = 0
    upstream_error = None  # B-07: set when upstream reports a mid-stream error
    # B-39: one-shot mid-stream fault accounting. A CLIENT DISCONNECT is not
    # an upstream fault, so the counters key off natural-EOF / exceptions only.
    body_exhausted = False
    gen_fault = False
    # P0-4: stateful special-token scrubbers (one per channel) — catch tokens
    # fragmented across chunks ('<un' + 'k>').
    _tok_text = _SpecialTokenFilter()
    _tok_reason = _SpecialTokenFilter()
    # R5 audit: DSML markup suppression on the visible text channel
    # (cross-chunk safe; complete markup collected for tool recovery).
    _dsml_text = _DsmlMarkupFilter()

    def _flush_text_events(idx: int):
        """P0-4: emit any filter-withheld text into its own channel before
        the block at `idx` closes."""
        out = []
        rest = _tok_text.flush()
        if rest:
            out.append(_sse('content_block_delta', {
                'type': 'content_block_delta', 'index': idx,
                'delta': {'type': 'text_delta', 'text': rest},
            }))
        return out

    def _flush_think_events(idx: int):
        out = []
        rest = _tok_reason.flush()
        if rest:
            out.append(_sse('content_block_delta', {
                'type': 'content_block_delta', 'index': idx,
                'delta': {'type': 'thinking_delta', 'thinking': rest},
            }))
        return out

    try:
        # message_start (always first)
        yield _sse('message_start', {
            'type': 'message_start',
            'message': {
                'id': msg_id, 'type': 'message', 'role': 'assistant',
                'model': model, 'content': [],
                'stop_reason': None, 'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0},
            },
        })

        # Buffer accumulator + idle-aware heartbeat.
        # Upstream chunks may contain multiple SSE lines or partial lines split
        # across chunk boundaries. Accumulate and split on \n to parse complete
        # lines only. Heartbeat fires on idle so reasoning models don't timeout.
        buffer = b''
        hb_interval = float(HEARTBEAT_MS) / 1000.0
        done = False
        # B-08 fix: sentinel-task idle detection (see common/sse.py).
        async for raw in _iter_chunks_with_idle(openai_gen, hb_interval):
            if done:
                break
            if raw is _IDLE:
                yield ': heartbeat\n\n'
                continue
            if isinstance(raw, str):
                raw = raw.encode('utf-8', errors='replace')
            buffer += raw
            # Parity fix: tolerate CRLF SSE framing (nous N-08).
            buffer = _normalize_sse_newlines(buffer)
            while b'\n' in buffer:
                line_bytes, buffer = buffer.split(b'\n', 1)
                line = line_bytes.decode('utf-8', errors='replace').strip()
                if not line:
                    continue

                # B-02 fix: accept `data:{...}` as well as `data: {...}` — the
                # space is optional per the SSE spec.
                if not line.startswith('data:'):
                    continue
                data_str = line[5:].strip()
                # B-01 parity: an empty `data:` is a keep-alive, not EOF.
                if not data_str:
                    continue
                if data_str == '[DONE]':
                    done = True
                    break

                try:
                    data = json.loads(data_str)
                except (json.JSONDecodeError, RecursionError):
                    continue

                # B-02 (compounding) fix: surface mid-stream upstream errors
                # instead of silently dropping them. Previously any payload
                # without object=="chat.completion.chunk" was discarded, so an
                # upstream {"error": {...}} vanished and the stream closed with
                # a fabricated end_turn.
                if isinstance(data.get('error'), (dict, str)):
                    err = data['error']
                    emsg = err.get('message') if isinstance(err, dict) else str(err)
                    logger.error(f'[openrouter] upstream error mid-stream: {emsg}')
                    upstream_error = emsg or 'upstream error'
                    done = True
                    break
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

                # Reasoning / thinking content — streamed as a thinking block
                # (parity with the shared AnthropicStreamState used by
                # nous/opencode/blackbox). Previously dropped entirely, so a
                # reasoning model's thinking never reached Claude Code on this
                # surface — part of the model's output silently vanished.
                reason_delta = (
                    delta.get('reasoning_content') if isinstance(delta.get('reasoning_content'), str)
                    else (delta.get('reasoning') if isinstance(delta.get('reasoning'), str) else '')
                )
                if reason_delta:
                    # P0-4: scrub tokenizer specials (incl. cross-chunk).
                    reason_delta = _tok_reason.feed(reason_delta)
                if reason_delta:
                    if not thinking_started:
                        # Close only an open TEXT block (tool blocks stay open
                        # concurrently — R-02). The next block gets a fresh
                        # index. P0-4: flush filter-withheld text first.
                        if block_open and text_started:
                            for _fe in _flush_text_events(block_index):
                                yield _fe
                            yield _sse('content_block_stop', {
                                'type': 'content_block_stop', 'index': block_index,
                            })
                            block_index += 1
                            text_started = False
                            block_open = False
                        thinking_index = block_index
                        yield _sse('content_block_start', {
                            'type': 'content_block_start', 'index': thinking_index,
                            # P1-2: thinking blocks require a `signature`.
                            'content_block': {'type': 'thinking', 'thinking': '', 'signature': ''},
                        })
                        thinking_started = True
                        block_open = True
                        block_index += 1
                    yield _sse('content_block_delta', {
                        'type': 'content_block_delta', 'index': thinking_index,
                        'delta': {'type': 'thinking_delta', 'thinking': reason_delta},
                    })

                # Text content
                _content_raw = delta.get('content')
                if isinstance(_content_raw, str) and _content_raw:
                    # R5: suppress DSML markup first (cross-chunk), then P0-4
                    # scrub tokenizer specials (incl. cross-chunk).
                    _content_raw = _tok_text.feed(_dsml_text.feed(_content_raw))
                else:
                    _content_raw = None
                if _content_raw:
                    if not text_started:
                        # Close an open thinking block if any (tool blocks stay
                        # open concurrently — R-02). P0-4: flush withheld
                        # thinking text first.
                        if block_open and thinking_started:
                            for _fe in _flush_think_events(thinking_index):
                                yield _fe
                            yield _sse('content_block_stop', {
                                'type': 'content_block_stop', 'index': thinking_index,
                            })
                            thinking_started = False
                            block_open = False
                        yield _sse('content_block_start', {
                            'type': 'content_block_start', 'index': block_index,
                            'content_block': {'type': 'text', 'text': ''},
                        })
                        text_started = True
                        block_open = True
                    yield _sse('content_block_delta', {
                        'type': 'content_block_delta', 'index': block_index,
                        'delta': {'type': 'text_delta', 'text': _content_raw},
                    })

                # Tool calls
                # B-03 fix (CRITICAL): three compounded defects previously
                # corrupted every tool call on this surface —
                #   1. content_block_start was emitted OUTSIDE the
                #      "first time we see this tool" guard, so it repeated on
                #      every delta chunk (and injected phantom unnamed blocks).
                #   2. the `if fn.get('arguments')` emit sat OUTSIDE the `for`
                #      loop, so with N parallel tools only the LAST tool's
                #      arguments were forwarded — the rest were lost entirely.
                #   3. block_index was never incremented for a tool block, so
                #      all parallel tools collided on the same index.
                # Verified by harness: 2 parallel tools produced 4 starts, all
                # at index 0, and dropped one tool's arguments completely.
                for tc in (delta.get('tool_calls') or []):
                    if not isinstance(tc, dict):
                        continue
                    tc_idx = tc.get('index', 0)
                    fn = tc.get('function') or {}
                    if tc_idx not in tool_call_blocks:
                        # R-02: close only a TEXT or THINKING block here.
                        # Closing the previous TOOL block orphaned its later
                        # argument fragments (OpenAI interleaves fragments
                        # across all active tool indices), so
                        # `content_block_delta` arrived on a closed index and
                        # Claude Code dropped the tool call. Tool blocks stay
                        # open concurrently and are all closed at the
                        # terminal path.
                        if block_open and text_started:
                            for _fe in _flush_text_events(block_index):
                                yield _fe
                            yield _sse('content_block_stop', {
                                'type': 'content_block_stop', 'index': block_index,
                            })
                            block_index += 1
                            text_started = False
                            block_open = False
                        elif block_open and thinking_started:
                            for _fe in _flush_think_events(thinking_index):
                                yield _fe
                            yield _sse('content_block_stop', {
                                'type': 'content_block_stop', 'index': thinking_index,
                            })
                            thinking_started = False
                            block_open = False
                        tool_call_blocks[tc_idx] = block_index
                        open_tool_blocks.add(block_index)
                        yield _sse('content_block_start', {
                            'type': 'content_block_start', 'index': block_index,
                            'content_block': {
                                'type': 'tool_use',
                                'id': tc.get('id') or f'toolu_{block_index}-{secrets.token_hex(3)}',
                                'name': fn.get('name', '') or '',
                                'input': {},
                            },
                        })
                        block_open = True
                        block_index += 1   # R-02: next block gets a fresh index
                    # Emit arguments for THIS tool (inside the loop).
                    if fn.get('arguments'):
                        yield _sse('content_block_delta', {
                            'type': 'content_block_delta',
                            'index': tool_call_blocks[tc_idx],
                            'delta': {'type': 'input_json_delta',
                                      'partial_json': fn['arguments']},
                        })

                if choice.get('finish_reason'):
                    finish_reason = choice['finish_reason']

        # Natural upstream EOF (the async-for above fell through) — distinct
        # from a client disconnect, which unwinds via GeneratorExit instead.
        body_exhausted = True

        # B-04 / R-02: close the open TEXT/THINKING block (if any), then every
        # concurrently-open tool_use block, lowest index first. P0-4: flush
        # any filter-withheld channel text BEFORE its block closes.
        if block_open and text_started:
            for _fe in _flush_text_events(block_index):
                yield _fe
            yield _sse('content_block_stop', {
                'type': 'content_block_stop', 'index': block_index,
            })
            # Next block gets a fresh index (parity with every other close
            # site — without this the R5 DSML drain below reused the just-
            # closed index: harness gate "content_block index reused after
            # close").
            block_index += 1
            text_started = False
        if block_open and thinking_started:
            for _fe in _flush_think_events(thinking_index):
                yield _fe
            yield _sse('content_block_stop', {
                'type': 'content_block_stop', 'index': thinking_index,
            })
            thinking_started = False
        for _ti in sorted(open_tool_blocks):
            yield _sse('content_block_stop', {
                'type': 'content_block_stop', 'index': _ti,
            })
        open_tool_blocks.clear()
        block_open = False

        # R5 audit: drain DSML-withheld clean text + re-emit complete DSML
        # tool markup recovered mid-stream as real tool_use blocks
        # (cross-wrapper stream/non-stream parity, CONTRACT §8).
        _dsml_rest = _tok_text.feed(_dsml_text.flush()) + _tok_text.flush()
        if _dsml_rest:
            yield _sse('content_block_start', {
                'type': 'content_block_start', 'index': block_index,
                'content_block': {'type': 'text', 'text': ''}})
            yield _sse('content_block_delta', {
                'type': 'content_block_delta', 'index': block_index,
                'delta': {'type': 'text_delta', 'text': _dsml_rest}})
            yield _sse('content_block_stop', {
                'type': 'content_block_stop', 'index': block_index})
            block_index += 1
        try:
            from common.translations import parse_dsml_from_text as _pdsml
            _clean_dsml, _dsml_tools = _pdsml(_dsml_text.collected_text or '')
        except Exception:
            _dsml_tools = []
        for _tu in _dsml_tools:
            if not isinstance(_tu, dict):
                continue
            try:
                _args_json = json.dumps(_tu.get('input') or {}, ensure_ascii=False)
            except Exception:
                _args_json = '{}'
            yield _sse('content_block_start', {
                'type': 'content_block_start', 'index': block_index,
                'content_block': {
                    'type': 'tool_use',
                    'id': _tu.get('id') or f'toolu_dsml_{block_index}-{secrets.token_hex(3)}',
                    'name': _tu.get('name') or '', 'input': {},
                }})
            yield _sse('content_block_delta', {
                'type': 'content_block_delta', 'index': block_index,
                'delta': {'type': 'input_json_delta', 'partial_json': _args_json}})
            yield _sse('content_block_stop', {
                'type': 'content_block_stop', 'index': block_index})
            block_index += 1

        # B-07 fix: an upstream error must NOT be reported as a successful
        # end_turn — the client would persist a truncated answer and could not
        # retry. Emit a real Anthropic `error` event first.
        if upstream_error:
            yield _sse('error', {
                'type': 'error',
                'error': {'type': 'api_error', 'message': str(upstream_error)[:2000]},
            })
        # P0-1 (CONTRACT §3.3): the upstream stream ended WITHOUT any terminal
        # signal (no finish_reason, no error frame). The old code closed with
        # a fabricated end_turn, so a truncated answer persisted as a
        # successful turn ("stops mid-way" symptom). Surface a real error.
        elif finish_reason is None:
            logger.error('[openrouter] upstream stream ended prematurely (no finish_reason)')
            yield _sse('error', {
                'type': 'error',
                'error': {'type': 'api_error',
                          'message': 'upstream stream ended prematurely: EOF without '
                                     'finish_reason or [DONE]; the response may be '
                                     'truncated — client may retry'},
            })

        # B-06 parity: map strictly from finish_reason; never infer tool_use
        # merely because a tool was seen earlier in the turn.
        # Audit 2026-08-03: finish_reason=None (premature EOF) → stop_reason
        # None; the `error` event above is the real failure signal (a claimed
        # end_turn would fabricate a clean completion).
        stop_reason = {'stop': 'end_turn', 'length': 'max_tokens', 'tool_calls': 'tool_use',
                       'content_filter': 'refusal'}.get(finish_reason, None)
        # R5 parity (shared anthropic_stream): DSML-recovered tool_use blocks
        # were emitted above but MiniMax reports finish_reason 'stop' for
        # those turns — upgrade end_turn → tool_use so Claude Code executes
        # the tool instead of closing the turn. A failed turn keeps
        # stop_reason None (CONTRACT §3.3, error event already emitted).
        if _dsml_tools and stop_reason == 'end_turn':
            stop_reason = 'tool_use'

        yield _sse('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': stop_reason, 'stop_sequence': None},
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens},
        })

        yield _sse('message_stop', {'type': 'message_stop'})

    except (GeneratorExit, asyncio.CancelledError):
        # B-09 parity: client disconnected. An async generator must not yield
        # during finalization — re-raise so cleanup happens in `finally` only.
        raise
    except Exception as e:
        logger.error(f'[openrouter] anthropic stream translation error: {e}')
        # B-07 fix: surface the failure instead of fabricating a clean turn.
        gen_fault = True
        try:
            if block_open and text_started:
                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': block_index})
            if block_open and thinking_started:
                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': thinking_index})
            for _ti in sorted(open_tool_blocks):
                yield _sse('content_block_stop', {'type': 'content_block_stop', 'index': _ti})
            open_tool_blocks.clear()
        except Exception:
            pass
        try:
            yield _sse('error', {
                'type': 'error',
                'error': {'type': 'api_error',
                          'message': f'upstream stream error: {str(e)[:2000]}'},
            })
            # stop_reason=None: failed turn; never claim end_turn.
            yield _sse('message_delta', {
                'type': 'message_delta',
                'delta': {'stop_reason': None, 'stop_sequence': None},
                'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens},
            })
            yield _sse('message_stop', {'type': 'message_stop'})
        except Exception:
            pass
    finally:
        # B-39 parity (CONTRACT §10): count mid-stream faults — an error event
        # after a committed HTTP 200 is invisible to per-status accounting.
        # Client disconnects are excluded (they are not upstream faults).
        if gen_fault or upstream_error is not None or (body_exhausted and finish_reason is None):
            try:
                metrics.record_error()
            except Exception:
                pass
        # B-09 fix: deterministically close the inner generator so the upstream
        # response is released and the pool key returned exactly once, even on
        # client disconnect. Previously this was left to GC, leaking in-flight
        # slots until the pool starved.
        try:
            aclose = getattr(openai_gen, 'aclose', None)
            if aclose is not None:
                await aclose()
        except Exception:
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


async def _mgmt_request(method: str, path: str = "", json_body: dict | None = None,
                          params: dict | None = None) -> Response:
    """Make authenticated management API request to OpenRouter."""
    if not MANAGEMENT_ENABLED:
        return _error_response(
            {"error": "OpenRouter management not enabled. Set OPENROUTER_MANAGEMENT_ENABLED=yes "
                       "and configure OPENROUTER_MANAGEMENT_KEY"},
            status_code=501,
        )
    mgmt_key = os.environ.get('OPENROUTER_MANAGEMENT_KEY', '').strip()
    if not mgmt_key:
        return _error_response(
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
            data = _sanitize_nonfinite(await resp.json())
            return JSONResponse(data, status_code=resp.status)
    except Exception as e:
        logger.error(f"[openrouter] Management API error: {e}")
        return _error_response({"error": str(e)}, status_code=502)


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
        return _error_response({"error": "old_key_hash is required"}, status_code=400)
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
        return _error_response({"error": "MCP not available"}, status_code=503)

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
        return _error_response({"error": "MCP not available"}, status_code=503)
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
        # CONTRACT §10: /health MUST report in-flight counts (was missing —
        # the only signal that detects a leaked pool reservation).
        "in_flight": sum(k.in_flight for k in pool.keys),
        "keys_status_detail": pool.all_stats(),
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
async def metrics_json():
    """CONTRACT §8/§10 parity fix: the four siblings serve the JSON metrics
    summary at /metrics and Prometheus exposition at /metrics/prom; openrouter
    served exposition on BOTH, so dashboards poll the wrong format and the
    per-key in-flight gauges were invisible to JSON clients."""
    s = await metrics.summary()
    s['pool'] = pool.all_stats()
    return s


@app.get("/metrics/prom")
async def prom_metrics():
    pool_metrics = pool.prom_metrics()
    req_metrics = metrics.prom_metrics()
    return Response(
        content=pool_metrics + "\n" + req_metrics,
        media_type="text/plain; version=0.0.4",
    )


@app.get("/metrics/model-status")
async def model_status():
    """P2 fix (fleet parity, audit 2026-08-03): every other wrapper exposes
    the per-model health/error state here; openrouter was the only one
    without it, so dashboards/ops checks had no visibility into which models
    were failing behind this wrapper."""
    return {
        "provider": "openrouter",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": await asyncio.to_thread(MODEL_STORE.status_map),
    }


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
