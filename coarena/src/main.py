#!/usr/bin/env python3
"""
Coarena API proxy server — OpenAI & Anthropic compatible.
Routes: /v1/chat/completions, /v1/responses, /v1/messages, /v1/models, /health
"""

import os
import sys
import json
import time
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional, Any, Dict, List

import aiohttp
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../coarena only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from common.middleware import RequestSizeLimiter, sanitize_header_value
    from common.sse import iter_chunks_with_idle
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

    def sanitize_header_value(value):
        # Fallback sanitizer: upstream common.middleware is missing from the
        # repo, so provide the BUG-SEC2 header-injection guard inline.
        # Strip control chars that could be used for header injection (CRLF etc.)
        if not isinstance(value, str):
            return value
        return ''.join(ch for ch in value if ord(ch) >= 32 or ch in '\t\n\r')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("coarena")

app = FastAPI(title="Coarena Wrapper", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request size limiter from common
if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter)

COARENA_BASE_URL = "https://coarena.ai/api/v1"
# Anthropic format uses /api/v1 not /api
COARENA_ANTHROPIC_BASE_URL = "https://coarena.ai/api/v1"

# Load API keys from env
API_KEYS = os.environ.get("COARENA_API_KEYS", "").split(",")
API_KEYS = [k.strip() for k in API_KEYS if k.strip()]
if not API_KEYS:
    # Fallback to single key
    single = os.environ.get("COARENA_API_KEY", "").strip()
    if single:
        API_KEYS = [single]

current_key_idx = 0
key_lock = asyncio.Lock()


def get_next_key() -> str:
    """Round-robin key selection."""
    global current_key_idx
    if not API_KEYS:
        raise HTTPException(status_code=503, detail="No Coarena API keys configured")
    key = API_KEYS[current_key_idx % len(API_KEYS)]
    current_key_idx += 1
    return key


async def proxy_request(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    params: Optional[Dict] = None,
    stream: bool = False,
) -> Any:
    """Proxy request to Coarena API with key rotation."""
    key = get_next_key()

    # Use Anthropic base for /messages endpoint
    if path == "/messages" or path == "/messages/count_tokens":
        url = f"{COARENA_ANTHROPIC_BASE_URL}{path}"
    else:
        url = f"{COARENA_BASE_URL}{path}"

    proxy_headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    # Forward relevant headers
    for h in ["anthropic-version", "anthropic-beta", "openai-beta"]:
        if h in headers:
            proxy_headers[h] = sanitize_header_value(headers[h])

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers=proxy_headers, data=body, params=params
        ) as resp:
            if stream:
                return StreamingResponse(
                    iter_chunks_with_idle(resp, 30.0),
                    status_code=resp.status,
                    headers=dict(resp.headers),
                    media_type=resp.content_type,
                )
            data = await resp.read()
            # Handle HTML error pages from Coarena
            if data and data.strip().startswith(b'<!DOCTYPE html>'):
                logger.error(f"Coarena returned HTML page for {path}: {data[:200]}")
                return JSONResponse(
                    content={"error": {"message": "Upstream returned HTML page, check endpoint", "type": "upstream_error"}},
                    status_code=502,
                )
            return JSONResponse(
                content=json.loads(data) if data else {},
                status_code=resp.status,
                headers=dict(resp.headers),
            )


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "coarena", "keys": len(API_KEYS)}


@app.get("/v1/models")
async def list_models():
    return await proxy_request("GET", "/models", {})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    # Check if streaming requested
    import json as json_lib
    try:
        body_json = json_lib.loads(body)
        stream = body_json.get("stream", False)
    except:
        stream = False
    return await proxy_request("POST", "/chat/completions", dict(request.headers), body, stream=stream)


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.body()
    # Check if streaming requested
    import json as json_lib
    try:
        body_json = json_lib.loads(body)
        stream = body_json.get("stream", False)
    except:
        stream = False
    return await proxy_request("POST", "/responses", dict(request.headers), body, stream=stream)


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.body()
    # Check if streaming requested
    import json as json_lib
    try:
        body_json = json_lib.loads(body)
        stream = body_json.get("stream", False)
    except:
        stream = False
    return await proxy_request("POST", "/messages", dict(request.headers), body, stream=stream)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    body = await request.body()
    return await proxy_request("POST", "/messages/count_tokens", dict(request.headers), body)


@app.get("/version")
async def version():
    return {"service": "coarena", "version": "1.0.0"}


@app.get("/props")
async def props():
    return {
        "service": "coarena",
        "upstream": "Coarena",
        "base_url": COARENA_BASE_URL,
        "anthropic_base_url": COARENA_ANTHROPIC_BASE_URL,
    }


# Catch-all for any other paths
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    body = await request.body()
    return await proxy_request(request.method, f"/{path}", dict(request.headers), body)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=9107, reload=False)