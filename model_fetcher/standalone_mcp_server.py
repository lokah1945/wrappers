#!/usr/bin/env python3
"""
Standalone MCP Server for Model Fetcher Catalog.

Runs independently on port 9300, exposing the NVIDIA NIM + multi-provider
catalog via FastMCP SSE transport. Can be used by external clients
(Claude Code, Cursor, etc.) without going through a wrapper.
"""

import json
import logging
import os
import sys
from pathlib import Path

# Add model_fetcher to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))  # wrapper root for common/

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.routing import APIRouter
from starlette.routing import Route
import anyio

from catalog_queries import (
    DEFAULT_DB,
    search_models,
    get_model,
    list_providers,
    search_provider_models,
    stats,
    open_db,
)

# Import catalog integration for routes
from common.catalog_integration import setup_catalog_routes

logger = logging.getLogger("model-fetcher-mcp")
logging.basicConfig(level=logging.INFO)

# Initialize FastMCP
mcp = FastMCP(
    "model-fetcher-catalog",
    instructions=(
        "AI Model Catalog - Search and inspect the audited NVIDIA NIM catalog "
        "and multi-provider model listings (OpenRouter, Nous, OpenCode, Blackbox). "
        "Provides real-time model availability, pricing, and capability data."
    ),
)

# Register MCP Tools

@mcp.tool(name="search_models")
async def mcp_search_models(
    query: str = "",
    modality: str = "",
    tier: str = "",
    working_only: bool = False,
    free_only: bool = False,
    publisher: str = "",
    limit: int = 50,
) -> str:
    """Search models in the catalog with filters."""
    db = open_db(DEFAULT_DB)
    if not db:
        return json.dumps({"error": "Catalog not available"})
    try:
        results = search_models(
            db, query=query or None, modality=modality or None,
            tier=tier or None, working_only=working_only,
            free_only=free_only, publisher=publisher or None,
            limit=limit,
        )
        return json.dumps({"count": len(results), "models": results}, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool(name="get_model")
async def mcp_get_model(catalog_id: str) -> str:
    """Get detailed info for a single model by catalog ID."""
    db = open_db(DEFAULT_DB)
    if not db:
        return json.dumps({"error": "Catalog not available"})
    try:
        result = get_model(db, catalog_id)
        return json.dumps(result or {"error": "Model not found"}, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool(name="list_providers")
async def mcp_list_providers() -> str:
    """List all providers in the catalog with model counts."""
    db = open_db(DEFAULT_DB)
    if not db:
        return json.dumps({"error": "Catalog not available"})
    try:
        result = list_providers(db)
        return json.dumps({"providers": result}, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool(name="search_provider_models")
async def mcp_search_provider_models(
    provider: str = "",
    query: str = "",
    free_only: bool = False,
    limit: int = 50,
) -> str:
    """Search models within a specific provider."""
    db = open_db(DEFAULT_DB)
    if not db:
        return json.dumps({"error": "Catalog not available"})
    try:
        results = search_provider_models(
            db, provider=provider or None, query=query or None,
            free_only=free_only, limit=limit,
        )
        return json.dumps({"count": len(results), "models": results}, ensure_ascii=False, indent=2)
    finally:
        db.close()


@mcp.tool(name="catalog_stats")
async def mcp_catalog_stats() -> str:
    """Get overall catalog statistics."""
    db = open_db(DEFAULT_DB)
    if not db:
        return json.dumps({"error": "Catalog not available"})
    try:
        result = stats(db)
        return json.dumps(result, ensure_ascii=False, indent=2)
    finally:
        db.close()


# SSE Transport Setup

sse_transport = SseServerTransport("/mcp/messages")

async def mcp_sse(request):
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


async def mcp_messages(request):
    return await sse_transport.handle_post_message(
        request.scope, request._receive, request._send
    )


# Health endpoint
async def health(request):
    db = open_db(DEFAULT_DB)
    if db:
        db.close()
        return JSONResponse({"ok": True, "db": "present"})
    return JSONResponse({"ok": False, "db": "missing"}, status_code=503)


# FastAPI app
app = FastAPI(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/mcp/sse", mcp_sse, methods=["GET"]),
        Route("/mcp/messages", mcp_messages, methods=["POST"]),
    ],
)

# Add catalog routes using APIRouter
catalog_router = APIRouter(prefix="")
setup_catalog_routes(catalog_router, prefix="")
app.include_router(catalog_router)

# Setup MCP server
from common.catalog_integration import setup_mcp_server
setup_mcp_server(app, "model-fetcher")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "9300"))
    host = os.environ.get("HOST", "0.0.0.0")
    logger.info(f"Starting Model Fetcher MCP Server on {host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")