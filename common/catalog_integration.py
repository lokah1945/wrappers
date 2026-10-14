#!/usr/bin/env python3
"""
catalog_integration.py — Shared catalog + MCP integration for all wrappers.

Provides drop-in FastAPI routes and FastMCP tools that any wrapper can mount.
This avoids duplicating the catalog integration code across 6 wrappers.

Usage (in any wrapper's main.py):
    from common.catalog_integration import (
        setup_catalog_routes,
        setup_mcp_server,
        free_only_enabled,
        is_free_model,
        CATALOG_DB_PATH,
    )

    # At module level:
    setup_catalog_routes(app)
    setup_mcp_server(app)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger('catalog-integration')

try:
    # FastAPI resolves route annotations against module globals, so the
    # Request type used by mcp_sse/mcp_messages MUST be importable at module
    # level. A function-local import leaves the annotation unresolvable and
    # FastAPI degrades the parameter to a (required) query param -> 422.
    from fastapi import Request as _FastAPIRequest
except ImportError:  # pragma: no cover - FastAPI is a hard runtime dep
    _FastAPIRequest = None

# ── Resolve model_fetcher path ──────────────────────────────────────────
# Try multiple common locations
_CANDIDATE_PATHS = [
    os.environ.get('CATALOG_REPO', ''),
    str(Path(__file__).resolve().parents[2] / 'model_fetcher'),
    str(Path(__file__).resolve().parents[3] / 'model_fetcher'),
    '/root/wrapper/model_fetcher',  # Primary: wrapper monorepo location
    '/root/project/model_fetcher',  # Actual deployment location (openrouter/.env CATALOG_REPO)
    '/root/model_fetcher',
    '/home/user/model_fetcher',
]

CATALOG_REPO = ''
for _p in _CANDIDATE_PATHS:
    # Check for catalog_queries.py in root or src/
    if _p and (os.path.isfile(os.path.join(_p, 'catalog_queries.py')) or 
               os.path.isfile(os.path.join(_p, 'src', 'catalog_queries.py'))):
        CATALOG_REPO = _p
        break

_HAS_CATALOG = False
_HAS_MANAGEMENT = False

if CATALOG_REPO:
    # Add both root and src/ to path
    if CATALOG_REPO not in sys.path:
        sys.path.insert(0, CATALOG_REPO)
    src_path = os.path.join(CATALOG_REPO, 'src')
    if os.path.isdir(src_path) and src_path not in sys.path:
        sys.path.insert(0, src_path)

try:
    from catalog_queries import (
        DEFAULT_DB,
        get_model,
        get_provider_model,
        open_db,
        search_models,
        search_provider_models,
    )
    from catalog_queries import list_providers as _list_providers
    from catalog_queries import stats as catalog_stats
    from env_config import free_only as _catalog_free_only

    CATALOG_DB_PATH = os.environ.get('CATALOG_DB',
        os.path.join(CATALOG_REPO, 'data', 'active_nvidia_nim.sqlite3'))
    _HAS_CATALOG = True
    logger.info(f"[catalog] Loaded from {CATALOG_REPO}, DB={CATALOG_DB_PATH}")
except ImportError as e:
    CATALOG_DB_PATH = os.environ.get('CATALOG_DB', '')
    logger.warning(f"[catalog] Not available: {e}")

# ── Provider Management ────────────────────────────────────────────────
try:
    import provider_management as MGT
    _HAS_MANAGEMENT = True
except ImportError:
    class _MgtStub:
        @staticmethod
        def is_management_enabled(*a, **kw): return False
        @staticmethod
        def get_provider_keys_status(*a, **kw): return {}
    MGT = _MgtStub()


# ── FREE_ONLY helpers ──────────────────────────────────────────────────

def free_only_enabled() -> bool:
    """Check if FREE_ONLY is enabled (env var or catalog config)."""
    v = (os.environ.get('FREE_ONLY') or 'no').strip().lower()
    if v in ('yes', 'true', '1', 'on', 'y'):
        return True
    try:
        return _catalog_free_only()
    except Exception:
        return False


def is_free_model(model_id: str) -> bool:
    """Check if a model ID is free (:free suffix or -free suffix)."""
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    return bool(mid.endswith((':free', '-free')))


# ── DB helper ──────────────────────────────────────────────────────────

def _get_catalog_db():
    """Open catalog database read-only, returns None on failure."""
    if not _HAS_CATALOG or not os.path.exists(CATALOG_DB_PATH):
        return None
    try:
        return open_db(CATALOG_DB_PATH)
    except FileNotFoundError:
        return None


def ngc_modality_map() -> dict:
    """NIM-taxonomy modalities per served model from the NGC catalog DB.

    Returns {model_id: [nim-modality, ...]} — e.g.
    {"nvidia/model": ["text/chat", "image/vision"]}. Empty dict when the
    catalog is unavailable; modalities are never guessed.
    """
    result: dict = {}
    try:
        db = _get_catalog_db()
    except Exception:
        return result
    if db is None:
        return result
    try:
        try:
            rows = db.execute(
                "SELECT served_model_id, modalities_json FROM models"
            ).fetchall()
        except Exception:
            return result
        for row in rows:
            try:
                mid = row["served_model_id"]
                raw = row["modalities_json"]
            except (IndexError, KeyError, TypeError):
                mid, raw = row[0], row[1]
            if not isinstance(mid, str) or not mid:
                continue
            try:
                mods = json.loads(raw or "[]")
            except (TypeError, ValueError):
                continue
            if isinstance(mods, list) and mods:
                result[mid] = [str(m) for m in mods if str(m)]
        return result
    finally:
        try:
            db.close()
        except Exception:
            pass


# ── FastMCP Server Setup ───────────────────────────────────────────────

def setup_mcp_server(app, wrapper_name: str = "wrapper") -> None:
    """Mount FastMCP SSE transport on /mcp/sse and /mcp.

    Args:
        app: FastAPI application instance.
        wrapper_name: Name for the MCP server (e.g. 'nvidia-catalog').
    """
    if not _HAS_CATALOG:
        logger.warning("[catalog] MCP server not available (catalog not loaded)")
        return

    try:
        import anyio
        from mcp.server.fastmcp import FastMCP
        from mcp.server.sse import SseServerTransport
        from common.auth import check_auth as _check_auth
        from starlette.responses import JSONResponse, StreamingResponse

        mcp = FastMCP(
            f"{wrapper_name}-catalog",
            instructions=(
                f"AI Model Catalog integrated with {wrapper_name}. "
                "Search and inspect the audited NVIDIA NIM catalog and "
                "multi-provider model listings (OpenRouter, Nous, OpenCode, "
                "Blackbox)."
            ),
        )

        # ── Register MCP tools ──
        @mcp.tool(name="search_nim_models")
        async def mcp_search_models(
            query: str = "", modality: str = "", tier: str = "",
            working_only: bool = False, free_only: bool = False,
            publisher: str = "", limit: int = 50,
        ) -> str:
            db = _get_catalog_db()
            if not db:
                return json.dumps({"error": "Catalog not available"})
            try:
                results = search_models(
                    db, query=query or None, modality=modality or None,
                    tier=tier or None, working_only=working_only,
                    free_only=free_only, publisher=publisher or None, limit=limit,
                )
                return json.dumps({"count": len(results), "models": results},
                                  ensure_ascii=False, indent=2)
            finally:
                db.close()

        @mcp.tool(name="get_nim_model")
        async def mcp_get_model(catalog_id: str) -> str:
            db = _get_catalog_db()
            if not db:
                return json.dumps({"error": "Catalog not available"})
            try:
                result = get_model(db, catalog_id)
                return json.dumps(result or {"error": "Model not found"},
                                  ensure_ascii=False, indent=2)
            finally:
                db.close()

        @mcp.tool(name="list_providers")
        async def mcp_list_providers() -> str:
            db = _get_catalog_db()
            if not db:
                return json.dumps({"error": "Catalog not available"})
            try:
                result = _list_providers(db)
                return json.dumps(result, ensure_ascii=False, indent=2)
            finally:
                db.close()

        @mcp.tool(name="search_provider_models")
        async def mcp_search_provider_models(
            provider: str = "", query: str = "",
            free_only: bool = False, limit: int = 50,
        ) -> str:
            db = _get_catalog_db()
            if not db:
                return json.dumps({"error": "Catalog not available"})
            try:
                results = search_provider_models(
                    db, provider=provider or None, query=query or None,
                    free_only=free_only, limit=limit,
                )
                return json.dumps({"count": len(results), "models": results},
                                  ensure_ascii=False, indent=2)
            finally:
                db.close()

        if _HAS_MANAGEMENT:
            @mcp.tool(name="openrouter_list_keys")
            async def mcp_list_keys(offset: int = 0) -> str:
                if not MGT.is_management_enabled("openrouter"):
                    return json.dumps({"error": "Management not enabled"})
                result = MGT.openrouter_list_keys(offset=offset)
                return json.dumps(result.data if result.success else {"error": result.error},
                                  ensure_ascii=False, indent=2, default=str)

            @mcp.tool(name="openrouter_key_usage")
            async def mcp_key_usage() -> str:
                if not MGT.is_management_enabled("openrouter"):
                    return json.dumps({"error": "Management not enabled"})
                result = MGT.openrouter_key_usage()
                return json.dumps(result.data if result.success else {"error": result.error},
                                  ensure_ascii=False, indent=2, default=str)

        # ── Mount SSE transport (raw ASGI, FastMCP sse_app pattern) ──
        # Starlette's Route treats *function* endpoints as request->Response
        # handlers, but a callable *instance* stays a raw ASGI app. That lets
        # connect_sse own the whole SSE response at the ASGI layer, exactly
        # like FastMCP's own sse_app() handle_sse. The previous construction
        # wrapped a coroutine in StreamingResponse — a coroutine is not an
        # async iterator, so the response body never started (client saw an
        # empty 200 stream, 'coroutine ... never awaited'), and the transport
        # was invoked with the anyio task group in the receive position
        # (SDK signature: connect_sse(scope, receive, send)).
        sse_transport = SseServerTransport("/mcp/messages")

        class _MCPASGIEndpoint:
            def __init__(self, kind, transport, mcp, check_auth):
                self._kind = kind
                self._transport = transport
                self._mcp = mcp
                self._check_auth = check_auth

            async def __call__(self, scope, receive, send):
                # B-31 parity: MCP transports can expose tools; protect them
                # like every other non-discovery agent surface. This still
                # accepts both Authorization: Bearer and x-api-key through
                # common.auth.
                req = _FastAPIRequest(scope, receive)
                if self._kind == 'sse':
                    auth = self._check_auth(req.headers, surface='/mcp/sse')
                else:
                    auth = self._check_auth(req.headers, surface='/mcp/messages')
                if not auth.ok:
                    err = JSONResponse(
                        {"error": {"message": auth.message, "type": "authentication_error"}},
                        status_code=auth.status)
                    await err(scope, receive, send)
                    return
                if self._kind == 'sse':
                    async with self._transport.connect_sse(
                            scope, receive, send) as streams:
                        await self._mcp._mcp_server.run(
                            streams[0], streams[1],
                            self._mcp._mcp_server.create_initialization_options())
                else:
                    await self._transport.handle_post_message(
                        scope, receive, send)

        from starlette.routing import Route as _StarletteRoute
        app.router.routes.append(_StarletteRoute(
            '/mcp/sse', endpoint=_MCPASGIEndpoint('sse', sse_transport, mcp, _check_auth),
            methods=['GET']))
        app.router.routes.append(_StarletteRoute(
            '/mcp/messages', endpoint=_MCPASGIEndpoint('post', sse_transport, None, _check_auth),
            methods=['POST']))

        logger.info("[catalog] MCP server mounted at /mcp/sse")
        return mcp

    except ImportError as e:
        logger.warning(f"[catalog] MCP not available: {e}")
        return None


# ── Catalog Routes ─────────────────────────────────────────────────────

def setup_catalog_routes(app, prefix: str = "") -> None:
    """Mount /catalog/* routes on a FastAPI app.

    Args:
        app: FastAPI application instance.
        prefix: Optional route prefix (default: /catalog).
    """
    if not _HAS_CATALOG:
        logger.warning("[catalog] Catalog routes not available")
        return

    from fastapi import APIRouter

    router = APIRouter(prefix=prefix or "/catalog", tags=["catalog"])

    @router.get("/health")
    async def catalog_health():
        if not _HAS_CATALOG:
            return {"ok": False, "reason": "not_loaded"}
        return {"ok": os.path.exists(CATALOG_DB_PATH), "db": "present" if os.path.exists(CATALOG_DB_PATH) else "missing"}

    @router.get("/stats")
    async def catalog_stats_route():
        db = _get_catalog_db()
        if not db:
            return {"error": "Catalog not available"}
        try:
            return catalog_stats(db)
        finally:
            db.close()

    @router.get("/providers")
    async def catalog_providers():
        db = _get_catalog_db()
        if not db:
            return {"error": "Catalog not available"}
        try:
            return {"providers": _list_providers(db)}
        finally:
            db.close()

    @router.get("/models")
    async def catalog_models(
        q: str = "", modality: str = "", tier: str = "",
        working_only: bool = False, free_only: bool = False,
        publisher: str = "", limit: int = 50,
    ):
        db = _get_catalog_db()
        if not db:
            return {"error": "Catalog not available"}
        try:
            results = search_models(
                db, query=q or None, modality=modality or None,
                tier=tier or None, working_only=working_only,
                free_only=free_only, publisher=publisher or None,
                limit=min(limit, 500),
            )
            return {"count": len(results), "models": results}
        finally:
            db.close()

    @router.get("/model")
    async def catalog_model(id: str = ""):
        if not id:
            return {"error": "Missing 'id' parameter"}
        db = _get_catalog_db()
        if not db:
            return {"error": "Catalog not available"}
        try:
            result = get_model(db, id)
            return result or {"error": "Model not found"}
        finally:
            db.close()

    @router.get("/provider-models")
    async def catalog_provider_models(
        provider: str = "", q: str = "",
        free_only: bool = False, limit: int = 50,
    ):
        db = _get_catalog_db()
        if not db:
            return {"error": "Catalog not available"}
        try:
            results = search_provider_models(
                db, provider=provider or None, query=q or None,
                free_only=free_only, limit=min(limit, 500),
            )
            return {"count": len(results), "models": results}
        finally:
            db.close()

    if _HAS_MANAGEMENT:
        @router.get("/keys-status")
        async def catalog_keys_status():
            return MGT.get_provider_keys_status()

    app.include_router(router)
    logger.info(f"[catalog] Routes mounted at {prefix or '/catalog'}")
