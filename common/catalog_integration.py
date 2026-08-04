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

# ── Resolve model_fetcher path ──────────────────────────────────────────
# Try multiple common locations
_CANDIDATE_PATHS = [
    os.environ.get('CATALOG_REPO', ''),
    str(Path(__file__).resolve().parents[2] / 'model_fetcher'),
    str(Path(__file__).resolve().parents[3] / 'model_fetcher'),
    '/root/wrapper/model_fetcher',  # Primary: wrapper monorepo location
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
                # B-22.2: clamp limit — SQLite LIMIT -1 means NO limit, so a
                # negative value would silently unbound the result set.
                results = search_models(
                    db, query=query or None, modality=modality or None,
                    tier=tier or None, working_only=working_only,
                    free_only=free_only, publisher=publisher or None,
                    limit=min(max(1, limit), 500),
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
                # B-22.2: clamp limit (negative LIMIT unbounds SQLite queries).
                results = search_provider_models(
                    db, provider=provider or None, query=query or None,
                    free_only=free_only, limit=min(max(1, limit), 500),
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
                result = MGT.openrouter_list_keys(offset=max(0, offset))
                return json.dumps(result.data if result.success else {"error": result.error},
                                  ensure_ascii=False, indent=2, default=str)

            @mcp.tool(name="openrouter_key_usage")
            async def mcp_key_usage() -> str:
                if not MGT.is_management_enabled("openrouter"):
                    return json.dumps({"error": "Management not enabled"})
                result = MGT.openrouter_key_usage()
                return json.dumps(result.data if result.success else {"error": result.error},
                                  ensure_ascii=False, indent=2, default=str)

        # ── Mount SSE transport ──
        sse_transport = SseServerTransport("/mcp/messages")

        @app.get("/mcp/sse")
        async def mcp_sse(request):
            # B-31 parity: MCP transports can expose tools; protect them like
            # every other non-discovery agent surface. This still accepts both
            # Authorization: Bearer and x-api-key through common.auth.
            auth = _check_auth(request.headers, surface='/mcp/sse')
            if not auth.ok:
                return JSONResponse({"error": {"message": auth.message, "type": "authentication_error"}}, status_code=auth.status)
            async def event_gen():
                async with anyio.create_task_group() as tg:
                    async with sse_transport.connect_sse(
                        request.scope, tg, request._receive
                    ) as streams:
                        await mcp._mcp_server.run(
                            streams[0], streams[1],
                            mcp._create_initialization_options()
                        )
            return StreamingResponse(event_gen(), media_type="text/event-stream")

        @app.post("/mcp/messages")
        async def mcp_messages(request):
            auth = _check_auth(request.headers, surface='/mcp/messages')
            if not auth.ok:
                return JSONResponse({"error": {"message": auth.message, "type": "authentication_error"}}, status_code=auth.status)
            return await sse_transport.handle_post_message(
                request.scope, request._receive, request._send
            )

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
                # B-22.2: clamp lower bound too — SQLite LIMIT -1 = unlimited.
                limit=min(max(1, limit), 500),
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
                # B-22.2: clamp lower bound too — SQLite LIMIT -1 = unlimited.
                free_only=free_only, limit=min(max(1, limit), 500),
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
