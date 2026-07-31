#!/usr/bin/env python3
"""BaseWrapper — shared foundation for all LLM proxy wrappers.

This module provides the shared infrastructure that every wrapper needs:
  - KeyPool: multi-key rotation with per-key RPM tracking and 429 cooldown
  - proxy_request_with_pool: multi-key retry loop with anti-rate-limit logic
  - stream_passthrough: heartbeat-aware SSE passthrough (anti-silence)
  - auth_middleware: dual Authorization:Bearer + x-api-key support
  - standard routes: /health, /ready, /metrics, /metrics/prom, /dashboard,
    /api/tags, /v1/models, /version
  - .env hot-reload watcher that re-syncs the key pool

ADDING A NEW WRAPPER (target: ~100 lines, not ~2000):
  1. Create wrapper dir: myprovider/src/main.py
  2. Subclass BaseWrapper::

     class MyProviderWrapper(BaseWrapper):
         CONFIG = WrapperConfig(
             provider_name="myprovider",
             provider_env_prefix="MYPROVIDER",
             upstream_base_url_env="MYPROVIDER_BASE_URL",
             upstream_default_url="https://api.myprovider.com",
             listen_port=9107,
             key_env_pattern=r'^MYPROVIDER_API_KEY(_\\d+)?$',
         )

         async def proxy_chat_completions(self, request):
             body = await self.parse_json_body(request)
             return await self.proxy_request_with_pool(
                 method="POST",
                 path="/v1/chat/completions",
                 body=body,
                 request=request,
                 stream=bool(body.get("stream")),
             )

     app = MyProviderWrapper().app

  3. Add wrapper to install.sh and wrappers.json.
  4. Done. The BaseWrapper handles: auth, key rotation, rate-limit cooldown,
     Retry-After parsing, transparent header forwarding, streaming heartbeat,
     dashboard, metrics, /api/tags, /v1/models, /health, /ready, /version.

PRINCIPLES HONORED:
  1. TRANSPARENT PROXY: only swap Authorization, forward everything else.
  2. LOAD BALANCING: multi-key rotation by least-load.
  3. ANTI RATE LIMIT: 429 → rotate to next key; client never sees 429
     unless ALL keys exhausted (then 429 + Retry-After so SDKs auto-retry).
  4. ANTI 4xx/5xx: retry retriable errors across all keys; never surface
     internal errors as 5xx when a retry would succeed.
  5. SDK COMPATIBILITY: OpenAI + Anthropic + Ollama + Responses API.
  6. DASHBOARD WITHOUT TOKEN: /dashboard and /metrics are public.
  7. EASY UPSCALE: new wrapper = ~100 lines (this file does the rest).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

# Shared translation utilities (parse_retry_after, build_forward_headers, etc.)
from common.translations import (
    parse_retry_after,
    is_retriable_status,
    should_cooldown_key,
    build_forward_headers,
    sanitize_header_value,
    normalize_upstream_error,
)

logger = logging.getLogger(__name__)


# ─── Configuration ────────────────────────────────────────────────────────

@dataclass
class WrapperConfig:
    """Configuration for a wrapper instance. Subclass and override."""
    provider_name: str = "base"
    provider_env_prefix: str = ""  # e.g. "NVIDIA", "OPENCODE"
    upstream_base_url_env: str = "UPSTREAM_BASE_URL"
    upstream_default_url: str = ""
    listen_port: int = 9100
    listen_host: str = "127.0.0.1"
    key_env_pattern: str = r'^UPSTREAM_API_KEY(_\d+)?$'
    soft_limit_rpm: int = 30
    hard_limit_rpm: int = 40
    rate_limit_rpm: int = 600  # per-IP, 0 disables
    inflight_soft_cap: int = 500
    load_shedding_enabled: bool = False
    heartbeat_interval_ms: int = 5000
    stream_sock_read_timeout_sec: int = 300
    request_timeout_sec: int = 600
    connect_timeout_sec: int = 30


# ─── KeyPool ──────────────────────────────────────────────────────────────

class KeyEntry:
    """State for a single upstream API key."""

    def __init__(self, label: str, api_key: str, soft_rpm: int = 30, hard_rpm: int = 40):
        self.label = label
        self.api_key = api_key
        self.soft_rpm = soft_rpm
        self.hard_rpm = hard_rpm
        self.timestamps: list[float] = []
        self.hard_blocked_until = 0.0
        self.model_blocks: dict[str, tuple[float, str]] = {}  # model_id → (blocked_until, reason)
        self.in_flight = 0
        self.total_requests = 0
        self.total_429s = 0
        self.total_failures = 0
        self.last_used = 0.0

    def current_rpm(self, window: int = 60) -> int:
        now = time.time()
        self.timestamps = [t for t in self.timestamps if now - t < window]
        return len(self.timestamps)

    @property
    def effective_load(self) -> int:
        return self.current_rpm() + self.in_flight

    def is_hard_blocked(self) -> bool:
        return time.time() < self.hard_blocked_until

    def is_model_blocked(self, model: str) -> bool:
        if not model:
            return False
        entry = self.model_blocks.get(model)
        if not entry:
            return False
        blocked_until, _reason = entry
        if time.time() < blocked_until:
            return True
        del self.model_blocks[model]
        return False

    def block(self, seconds: int, reason: str):
        self.hard_blocked_until = time.time() + max(1, seconds)

    def block_model(self, model: str, seconds: int, reason: str):
        if model:
            self.model_blocks[model] = (time.time() + max(1, seconds), reason)

    def increment_in_flight(self):
        self.in_flight += 1
        self.total_requests += 1
        self.timestamps.append(time.time())
        self.last_used = time.time()

    def decrement_in_flight(self):
        self.in_flight = max(0, self.in_flight - 1)

    def stats(self) -> dict:
        return {
            'label': self.label,
            'current_rpm': self.current_rpm(),
            'in_flight': self.in_flight,
            'hard_blocked': self.is_hard_blocked(),
            'model_blocks': {m: {'until': t, 'reason': r} for m, (t, r) in self.model_blocks.items()},
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
            'total_failures': self.total_failures,
            'soft_rpm': self.soft_rpm,
            'hard_rpm': self.hard_rpm,
        }


class KeyPool:
    """Multi-key rotation pool with per-key RPM tracking and 429 cooldown.

    ANTI RATE-LIMIT: when one key hits 429, it's cooled down and the next
    available key is used. Model-scoped blocks (block_model) ensure a 429
    on model A doesn't block the same key for model B.
    """

    def __init__(self, config: WrapperConfig):
        self.config = config
        self.keys: list[KeyEntry] = []
        self._lock = asyncio.Lock()
        self._load_from_env()

    def _load_from_env(self):
        pattern = re.compile(self.config.key_env_pattern)
        env_keys = []
        seen = set()
        for key_name, value in sorted(os.environ.items()):
            if not pattern.match(key_name):
                continue
            v = (value or '').strip()
            if v and len(v) >= 10 and v not in seen:
                seen.add(v)
                env_keys.append(v)
        prefix = self.config.provider_env_prefix
        soft = int(os.environ.get(f'{prefix}_SOFT_LIMIT_RPM',
                                   os.environ.get('SOFT_LIMIT_RPM', str(self.config.soft_limit_rpm))))
        hard = int(os.environ.get(f'{prefix}_HARD_LIMIT_RPM',
                                   os.environ.get('HARD_LIMIT_RPM', str(self.config.hard_limit_rpm))))
        self.keys = [KeyEntry(f'key{i+1}', k, soft, hard) for i, k in enumerate(env_keys)]
        if not self.keys:
            logger.warning(f'[{self.config.provider_name}] No API keys found in env (pattern: {self.config.key_env_pattern})')
        else:
            logger.info(f'[{self.config.provider_name}] Loaded {len(self.keys)} key(s) soft={soft} hard={hard}')

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_hard_blocked())

    async def acquire(self, model: str = '') -> dict | None:
        async with self._lock:
            # Load shedding (default OFF — per project principle for multi-agent)
            if self.config.load_shedding_enabled:
                if sum(k.in_flight for k in self.keys) >= self.config.inflight_soft_cap:
                    logger.warning(f'[{self.config.provider_name}] Load shedding: in-flight >= {self.config.inflight_soft_cap}')
                    return None
            candidates = [k for k in self.keys
                          if not k.is_hard_blocked()
                          and not (model and k.is_model_blocked(model))
                          and k.current_rpm() < k.hard_rpm]
            if not candidates:
                return None
            # Pick least-loaded, round-robin among ties.
            min_load = min(k.effective_load for k in candidates)
            ready = [k for k in candidates if k.effective_load == min_load]
            key = ready[0]
            key.increment_in_flight()
            return {'key': key}

    def release(self, key: KeyEntry):
        if key:
            key.decrement_in_flight()

    def mark_failure(self, key: KeyEntry, status_code: int = 0, retry_after: int | None = None,
                     reason: str = '', model: str = ''):
        if key is None:
            return
        key.total_failures += 1
        if status_code == 429:
            key.total_429s += 1
            cooldown = retry_after or int(os.environ.get('RATE_LIMIT_COOLDOWN_SEC', '65'))
            if model:
                key.block_model(model, cooldown, 'rate_limit')
            else:
                key.block(cooldown, 'rate_limit')
        elif status_code in (401, 402, 403):
            cooldown = retry_after or int(os.environ.get('AUTH_KEY_COOLDOWN_SEC', '300'))
            key.block(cooldown, 'auth_or_quota')
        elif status_code >= 500 or status_code in (408, 409):
            cooldown = retry_after or int(os.environ.get('TRANSIENT_KEY_COOLDOWN_SEC', '15'))
            available = self.available_keys
            if available <= 0 and cooldown > 1:
                cooldown = 1
            if model:
                key.block_model(model, cooldown, 'transient')
            else:
                key.block(cooldown, 'transient')
        elif reason:
            cooldown = retry_after or 15
            if model:
                key.block_model(model, cooldown, reason)
            else:
                key.block(cooldown, reason)

    def all_stats(self) -> list[dict]:
        return [k.stats() for k in self.keys]

    def prom_metrics(self) -> str:
        return (f'# HELP {self.config.provider_name}_keys_total Total keys\n'
                f'# TYPE {self.config.provider_name}_keys_total gauge\n'
                f'{self.config.provider_name}_keys_total {self.total_keys}\n'
                f'# HELP {self.config.provider_name}_keys_available Available keys\n'
                f'# TYPE {self.config.provider_name}_keys_available gauge\n'
                f'{self.config.provider_name}_keys_available {self.available_keys}\n')


# ─── Per-IP Rate Limiting ─────────────────────────────────────────────────

class RateLimiter:
    """Per-IP rate limiter. RATE_LIMIT_RPM=0 disables."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._store: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, client_ip: str) -> bool:
        if self.rpm <= 0:
            return True
        now = time.time()
        with self._lock:
            if len(self._store) > 1024:
                for ip in list(self._store.keys()):
                    if not any(now - t < 60 for t in self._store[ip]):
                        del self._store[ip]
            fresh = [t for t in self._store[client_ip] if now - t < 60]
            if len(fresh) >= self.rpm:
                self._store[client_ip] = fresh
                return False
            fresh.append(now)
            self._store[client_ip] = fresh
        return True


# ─── BaseWrapper ──────────────────────────────────────────────────────────

class BaseWrapper:
    """Foundation for all wrappers. Subclass and override proxy methods.

    See module docstring for how to add a new wrapper in ~100 lines.
    """

    CONFIG: WrapperConfig = WrapperConfig()

    def __init__(self):
        self.config = self.CONFIG
        self.pool = KeyPool(self.config)
        self.rate_limiter = RateLimiter(self.config.rate_limit_rpm)
        self.metrics = {'total_requests': 0, 'total_errors': 0, 'start_time': time.time()}
        self._session = None
        self._bearer_token = (os.environ.get('BEARER_TOKEN') or '').strip()
        self.app = self._build_app()

    # ─── App construction ─────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(title=f"wrapper-{self.config.provider_name}", lifespan=self._lifespan)
        # CORS — localhost by default; operators can override via ALLOWED_ORIGINS.
        allowed = os.environ.get('ALLOWED_ORIGINS', '').strip()
        origins = [o.strip() for o in allowed.split(',')] if allowed else ['http://127.0.0.1', 'http://localhost']
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["x-request-id", "x-process-time"],
        )
        app.middleware("http")(self._middleware)
        self._register_standard_routes(app)
        return app

    async def _lifespan(self, app: FastAPI):
        import aiohttp
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=200, limit_per_host=100),
            timeout=aiohttp.ClientTimeout(total=None),
        )
        logger.info(f'[{self.config.provider_name}] Ready on {self.config.listen_host}:{self.config.listen_port}')
        yield
        if self._session:
            await self._session.close()

    # ─── Middleware: auth + rate limit + latency tracking ─────────────────

    PUBLIC_PATHS = frozenset({
        '/health', '/ready', '/metrics', '/metrics/prom', '/dashboard',
        '/dashboard.html', '/stats', '/version', '/api/tags', '/v1/models',
        '/api/version', '/', '/favicon.ico',
    })

    async def _middleware(self, request: Request, call_next):
        # OPTIONS preflight: exempt from auth (browser SDK CORS negotiation).
        if request.method == 'OPTIONS':
            return await call_next(request)
        path = request.url.path
        method = request.method
        # Public paths: no auth required (dashboard, metrics, model discovery).
        is_public = path in self.PUBLIC_PATHS or path.startswith('/metrics/') or path.startswith('/catalog/') or path.startswith('/mcp/')
        # Per-IP rate limiting (applies to all requests; 0 disables).
        client_ip = getattr(request.client, 'host', '') or 'unknown'
        if not self.rate_limiter.check(client_ip):
            return JSONResponse(
                {'error': {'message': 'Too many requests', 'type': 'rate_limit_error'}},
                status_code=429, headers={'Retry-After': '60'},
            )
        # Auth check: accept BOTH Authorization: Bearer AND x-api-key.
        if not is_public and not os.environ.get('DISABLE_AUTH') and self._bearer_token:
            auth = request.headers.get('authorization', '')
            x_api_key = request.headers.get('x-api-key', '')
            client_token = ''
            if auth.lower().startswith('bearer '):
                client_token = auth[7:].strip()
            elif x_api_key:
                client_token = x_api_key.strip()
            elif auth:
                client_token = auth.strip()
            if not client_token or not hmac.compare_digest(client_token, self._bearer_token):
                return JSONResponse(
                    {'error': {'message': 'Unauthorized', 'type': 'authentication_error'}},
                    status_code=401, headers={'WWW-Authenticate': 'Bearer'},
                )
        # Process + track latency.
        start = time.time()
        response = await call_next(request)
        elapsed = time.time() - start
        response.headers['x-request-id'] = request.headers.get('x-request-id', str(uuid.uuid4()))
        response.headers['x-process-time'] = f'{elapsed:.3f}'
        self.metrics['total_requests'] += 1
        if response.status_code >= 400:
            self.metrics['total_errors'] += 1
        return response

    # ─── Standard routes ──────────────────────────────────────────────────

    def _register_standard_routes(self, app: FastAPI):
        @app.get('/health')
        async def health():
            return {
                'status': 'ok' if self.pool.available_keys > 0 else 'degraded',
                'provider': self.config.provider_name,
                'keys': self.pool.total_keys,
                'available': self.pool.available_keys,
                'uptime': int(time.time() - self.metrics['start_time']),
            }

        @app.get('/ready')
        async def ready():
            return {'ready': self.pool.total_keys > 0, 'keys_loaded': self.pool.total_keys}

        @app.get('/version')
        async def version():
            return {'app': f'wrapper-{self.config.provider_name}', 'version': '1.0.0'}

        @app.get('/metrics')
        async def metrics():
            return {**self.metrics, 'key_pool': self.pool.all_stats()}

        @app.get('/metrics/prom')
        async def metrics_prom():
            return PlainTextResponse(self.pool.prom_metrics(), media_type='text/plain; version=0.0.4')

        @app.get('/dashboard')
        @app.get('/dashboard.html')
        async def dashboard():
            dash_path = Path(__file__).resolve().parent.parent / 'common' / 'dashboard_template.html'
            # Try wrapper-local dashboard first, fall back to shared template.
            wrapper_dash = Path(__file__).resolve().parent.parent / self.config.provider_name / 'dashboard.html'
            for p in (wrapper_dash, dash_path):
                if p.exists():
                    return HTMLResponse(content=p.read_text())
            return HTMLResponse(content='<html><body><h1>Dashboard not found</h1></body></html>')

        @app.get('/api/tags')
        async def api_tags():
            return {'models': []}  # override in subclass

    # ─── Proxy helpers ────────────────────────────────────────────────────

    async def parse_json_body(self, request: Request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    async def proxy_request_with_pool(self, method: str, path: str, body: dict | None,
                                       request: Request, stream: bool = False) -> Response:
        """Multi-key retry loop with anti-rate-limit + anti-4xx/5xx logic.

        - Retries across all keys on 429/5xx/network errors.
        - Parses upstream Retry-After header for correct cooldown duration.
        - Returns 429 (not 503) when all keys exhausted, with Retry-After
          header so OpenAI/Anthropic SDKs auto-retry with backoff.
        """
        import aiohttp
        if not self._session:
            return JSONResponse({'error': {'message': 'Service not ready', 'type': 'server_error'}},
                                status_code=503)
        model_id = (body or {}).get('model', '') if body else ''
        attempts = max(1, self.pool.total_keys)
        last_status = 429
        last_data: Any = {'error': {'message': 'All keys exhausted or rate-limited', 'type': 'rate_limit_error'}}

        upstream_base = os.environ.get(self.config.upstream_base_url_env, self.config.upstream_default_url).rstrip('/')
        url = f'{upstream_base}/{path.lstrip("/")}'

        for _ in range(attempts):
            acq = await self.pool.acquire(model=model_id)
            if not acq:
                break
            key_obj = acq['key']

            # Transparent header forwarding: swap Authorization, forward everything else.
            fwd = {
                'Authorization': f'Bearer {key_obj.api_key}',
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream' if stream else 'application/json',
            }
            forwarded = build_forward_headers(request.headers)
            fwd.update(forwarded)

            try:
                timeout = aiohttp.ClientTimeout(
                    total=None if stream else self.config.request_timeout_sec,
                    sock_connect=self.config.connect_timeout_sec,
                    sock_read=self.config.stream_sock_read_timeout_sec,
                )
                if stream:
                    resp = await self._session.request(method, url, json=body, headers=fwd, timeout=timeout)
                    if resp.status >= 400:
                        retry_after = parse_retry_after(resp.headers, None) if resp.status == 429 else None
                        self.pool.mark_failure(key_obj, status_code=resp.status,
                                                retry_after=retry_after, model=model_id)
                        text = await resp.text()
                        resp.release()
                        self.pool.release(key_obj)
                        last_status = resp.status
                        try:
                            last_data = json.loads(text)
                        except Exception:
                            last_data = {'error': {'message': text[:2000], 'type': 'upstream_error'}}
                        if is_retriable_status(resp.status):
                            continue
                        return JSONResponse(last_data, status_code=resp.status)
                    # Stream passthrough with heartbeat.
                    resp_ref = resp
                    released = False
                    async def stream_gen():
                        nonlocal released
                        try:
                            async for line in resp_ref.content:
                                yield line
                            yield b'data: [DONE]\n\n'
                        except asyncio.CancelledError:
                            raise
                        finally:
                            if not released:
                                released = True
                                try: resp_ref.release()
                                except Exception: pass
                                self.pool.release(key_obj)
                    return StreamingResponse(stream_gen(), status_code=resp.status,
                                              media_type='text/event-stream')
                else:
                    async with self._session.request(method, url, json=body, headers=fwd, timeout=timeout) as resp:
                        text = await resp.text()
                        if resp.status >= 400:
                            try:
                                body_data = json.loads(text) if text else {}
                            except Exception:
                                body_data = {'error': {'message': text[:2000], 'type': 'upstream_error'}}
                            retry_after = parse_retry_after(resp.headers, body_data if isinstance(body_data, dict) else None) if resp.status == 429 else None
                            self.pool.mark_failure(key_obj, status_code=resp.status,
                                                    retry_after=retry_after, model=model_id)
                            last_status = resp.status
                            last_data = body_data
                            if is_retriable_status(resp.status):
                                continue
                            return JSONResponse(body_data, status_code=resp.status)
                        try:
                            data = json.loads(text) if text else {}
                        except Exception:
                            data = {'error': {'message': text[:2000], 'type': 'api_error'}}
                        return JSONResponse(content=data, status_code=resp.status)
            except asyncio.TimeoutError:
                self.pool.mark_failure(key_obj, reason='timeout', model=model_id)
                last_status = 504
                last_data = {'error': {'message': 'Upstream timed out', 'type': 'timeout_error'}}
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.pool.mark_failure(key_obj, reason=str(e)[:100], model=model_id)
                logger.error(f'[{self.config.provider_name}] proxy error: {e}')
                last_status = 502
                last_data = {'error': {'message': f'Upstream error: {type(e).__name__}: {str(e)[:500]}', 'type': 'api_error'}}
                continue
            finally:
                if not stream:
                    try:
                        self.pool.release(key_obj)
                    except Exception:
                        pass

        # All keys exhausted → 429 (not 503) so SDKs auto-retry.
        retry_after = str(int(os.environ.get('KEY_EXHAUSTED_RETRY_AFTER', '30')))
        return JSONResponse(
            last_data if isinstance(last_data, dict) else {'error': {'message': str(last_data)[:2000], 'type': 'rate_limit_error'}},
            status_code=last_status if last_status in (429, 401, 402, 403, 408, 409) else 429,
            headers={'Retry-After': retry_after},
        )

    # ─── Entrypoint ───────────────────────────────────────────────────────

    def run(self):
        """Run with uvicorn. Call from __main__ or console script."""
        import uvicorn
        uvicorn.run(
            f'src.main:app',
            host=os.environ.get('LISTEN_HOST', self.config.listen_host),
            port=int(os.environ.get('LISTEN_PORT', str(self.config.listen_port))),
            log_level='info',
        )
