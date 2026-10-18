"""Read-only dashboard gateway. Provider credentials never enter this process."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from common.sse import ClosingStreamingResponse as StreamingResponse
from common.wrapper_scope import WrapperScopeError, load_wrappers, production_wrappers

ROOT = Path(__file__).resolve().parents[1]
HTML = Path(__file__).with_name("index.html")
MAX_BODY = 4 * 1024 * 1024
PROXY_PATHS = frozenset({
    "/health", "/ready", "/stats", "/version",
    "/api/version", "/events", "/metrics", "/metrics/prom", "/metrics/tokens",
    "/metrics/models", "/metrics/models/timeseries", "/metrics/keys",
    "/metrics/activity", "/metrics/rate-limits", "/metrics/model-status",
    "/metrics/chart/hourly", "/metrics/chart/daily", "/v1/models",
    "/v1/capabilities", "/catalog/health", "/catalog/stats", "/catalog/providers",
    "/catalog/models", "/catalog/model", "/catalog/provider-models",
})
NUMERIC_FIELDS = frozenset({
    "keys", "available", "keys_loaded", "total_keys", "available_keys", "models_cached", "uptime", "uptime_seconds",
    "total_requests", "requests", "errors", "total_errors", "success_count",
    "error_count", "avg_latency_ms", "p50_latency_ms", "p95_latency_ms",
    "input_tokens", "output_tokens", "total_tokens", "tokens_in", "tokens_out",
    "requests_total", "errors_total", "success_rate", "error_rate", "rate_limits",
})
SECRET_QUERY = re.compile(r"token|api[-_]?key|authorization|auth|secret|password|credential", re.I)
AUTH_HEADER_NAMES = frozenset({"authorization", "x-api-key"})


def forwarded_auth(request: Request) -> dict[str, str]:
    """Forward only operator-supplied wrapper credentials to read probes.

    The dashboard never loads provider .env files. A browser may optionally
    provide the same bearer/API-key header it would use in a wrapper dashboard;
    the value is kept in transit only and is never included in status JSON.
    """
    return {
        key.lower(): value for key, value in request.headers.items()
        if key.lower() in AUTH_HEADER_NAMES and value
    }


def auth_scope(headers: dict[str, str]) -> str:
    """Return a non-secret cache namespace for the supplied read credential."""
    if not headers:
        return "anonymous"
    material = "\x00".join(f"{key}:{headers[key]}" for key in sorted(headers))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def collection_count(body: object, *keys: str) -> int | None:
    if isinstance(body, list):
        return len(body)
    if not isinstance(body, dict):
        return None
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return len(value)
    return None


# The unified feed may contain provider model metadata and activity records, but
# it must never turn a dashboard into a credential or prompt exfiltration path.
SENSITIVE_FIELDS = frozenset({
    "token", "tokens", "live_keys", "api_key", "api_keys", "key_pool",
    "authorization", "credentials", "password", "secret", "source_root",
    "prompt", "messages", "request_headers", "response_headers",
    "error", "errors", "detail", "message", "last_error", "reason_detail",
})


def public_json(value: object, *, depth: int = 0, max_items: int = 250,
                parent_key: str = "") -> object:
    """Bound and redact JSON-shaped provider metadata for the global feed.

    Provider pricing is safe model metadata, not a user prompt. Preserve its
    numeric/string ``prompt`` price for honest model-detail reporting, but do
    not treat a zero text price as proof that every modality is free; wrappers
    apply their own explicit model policy.
    """
    if depth > 5:
        return None
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            key_text = str(key)
            if key_text.lower() in SENSITIVE_FIELDS:
                # Numeric aggregate counters remain useful telemetry; string,
                # mapping, and list values under these names are redacted.
                pricing_field = (parent_key.lower() == "pricing"
                                 and key_text.lower() in {"prompt", "completion", "input", "output"}
                                 and isinstance(item, (int, float, str))
                                 and not isinstance(item, bool))
                if not (pricing_field
                        or (key_text.lower() in {"error", "errors", "tokens"}
                            and isinstance(item, (int, float))
                            and not isinstance(item, bool))):
                    continue
            result[key_text] = public_json(item, depth=depth + 1,
                                           max_items=max_items, parent_key=key_text)
        return result
    if isinstance(value, list):
        return [public_json(item, depth=depth + 1, max_items=max_items)
                for item in value[:max_items]]
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:500]


def public_collection(body: object, *keys: str, max_items: int = 250) -> list:
    if isinstance(body, list):
        value = body
    elif isinstance(body, dict):
        value = next((body.get(key) for key in keys if isinstance(body.get(key), list)), [])
    else:
        value = []
    result = public_json(value[:max_items], max_items=max_items)
    return result if isinstance(result, list) else []


def metadata(repo: Path) -> tuple[dict, tuple[str, ...]]:
    try:
        entries = load_wrappers(repo)
        active = tuple(sorted(
            (name for name in production_wrappers(repo)
             if name != "dashboard" and entries[name].get("service_kind") != "dashboard"),
            key=lambda name: (entries[name].get("port", 65536), name),
        ))
    except WrapperScopeError as exc:
        raise HTTPException(503, "Authoritative wrapper scope is unavailable") from exc
    for name in active:
        port = entries[name].get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise HTTPException(503, f"Invalid port metadata for {name}")
    return entries, active


def safe_numbers(body: object) -> dict:
    """Copy only explicitly permitted numeric counters, never key lists/errors."""
    if not isinstance(body, dict):
        return {}
    out = {}
    for container in (body, body.get("metrics"), body.get("summary"), body.get("totals"), body.get("key_pool")):
        if not isinstance(container, dict):
            continue
        for key in NUMERIC_FIELDS:
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                out[key] = value
    return out


def model_policy(name: str, entry: dict, *bodies: object) -> dict:
    """Describe the live wrapper's FREE_ONLY display/request policy.

    The dashboard must not infer that a catalog row is allowed merely because
    it exists in the broad model-fetcher catalog. NVIDIA's wrapper is an
    inherently free provider surface; Nous uses its suffix/allowlist policy;
    OpenRouter reports exact zero pricing for some suffix-less free models.
    """
    reported = None
    for body in bodies:
        if isinstance(body, dict) and isinstance(body.get("free_only"), bool):
            reported = body["free_only"]
            break
    upstream = str(entry.get("upstream", ""))
    if name == "nvidia-python" or upstream.lower() == "nvidia nim":
        return {
            "free_only": True,
            "mode": "provider_inherent_free",
            "source": "NVIDIA NIM wrapper contract",
        }
    if reported is True:
        mode = "provider_free_suffix_only" if name == "openrouter" else "free_suffix_or_allowlist"
        return {"free_only": True, "mode": mode, "source": "live wrapper policy"}
    if reported is False:
        return {"free_only": False, "mode": "paid_and_free_allowed", "source": "live wrapper policy"}
    return {"free_only": None, "mode": "not_reported", "source": "wrapper did not report policy"}


def bridge_html(body: str, name: str) -> str:
    """Prefix existing dashboard requests and isolate each wrapper's browser state."""
    prefix = json.dumps(f"/wrappers/{name}")
    script = """<script data-unified-dashboard-bridge>
(() => {
 const prefix = PREFIX;
 const rewrite = input => {
   const value = input instanceof Request ? input.url : String(input);
   const url = new URL(value, window.location.href);
   if (url.origin !== window.location.origin) return value;
   if (!url.pathname.startsWith(prefix + '/')) url.pathname = prefix + url.pathname;
   for (const key of [...url.searchParams.keys()])
     if (/token|api[-_]?key|authorization|auth|secret|password|credential/i.test(key)) url.searchParams.delete(key);
   return url.href;
 };
 const nativeFetch = window.fetch.bind(window);
 window.fetch = (input, init) => nativeFetch(input instanceof Request ? new Request(rewrite(input), input) : rewrite(input), init);
 const NativeEventSource = window.EventSource;
 if (NativeEventSource) {
   window.EventSource = function(input, options) { return new NativeEventSource(rewrite(input), options); };
   window.EventSource.prototype = NativeEventSource.prototype;
   Object.setPrototypeOf(window.EventSource, NativeEventSource);
 }
 const nativeOpen = XMLHttpRequest.prototype.open;
 XMLHttpRequest.prototype.open = function(method, url, ...rest) { return nativeOpen.call(this, method, rewrite(url), ...rest); };
 const storagePrefix = 'unified:' + prefix + ':';
 for (const method of ['getItem', 'setItem', 'removeItem']) {
   const native = Storage.prototype[method];
   Storage.prototype[method] = function(key, ...rest) { return native.call(this, storagePrefix + String(key), ...rest); };
 }
})();
</script>""".replace("PREFIX", prefix)
    match = re.search(r"<head(?:\s[^>]*)?>", body, re.I)
    return body[:match.end()] + script + body[match.end():] if match else script + body


async def bounded_read(response, limit: int = MAX_BODY) -> bytes:
    chunks = []
    size = 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            raise HTTPException(502, "Wrapper response exceeds dashboard limit")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(repo: Path = ROOT, *, session_factory=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application):
        factory = session_factory or aiohttp.ClientSession
        application.state.http = factory(
            timeout=aiohttp.ClientTimeout(total=4, connect=1.5, sock_read=3),
            # Each of the independent polling, proxy and SSE pools permits
            # eight requests; transport capacity must permit all three pools.
            connector=aiohttp.TCPConnector(limit=24), trust_env=False,
        )
        application.state.slots = asyncio.Semaphore(8)
        application.state.poll_slots = asyncio.Semaphore(8)
        application.state.stream_slots = asyncio.Semaphore(8)
        application.state.cache_lock = asyncio.Lock()
        application.state.cached = None
        application.state.refresh_tasks = {}
        try:
            yield
        finally:
            refresh_tasks = list(application.state.refresh_tasks.values())
            for task in refresh_tasks:
                task.cancel()
            if refresh_tasks:
                await asyncio.gather(*refresh_tasks, return_exceptions=True)
            await application.state.http.close()

    application = FastAPI(title="Wrapper dashboard", lifespan=lifespan, redirect_slashes=False)

    @application.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @application.get("/health")
    async def health():
        metadata(repo)
        return {"status": "ok", "service": "wrapper-dashboard", "read_only": True}

    @application.get("/ready")
    async def ready():
        metadata(repo)
        return {"ready": True, "service": "wrapper-dashboard", "scope": "dashboard_service"}

    @application.get("/", response_class=HTMLResponse)
    @application.get("/dashboard", response_class=HTMLResponse)
    async def home():
        metadata(repo)
        return HTMLResponse(HTML.read_text())

    async def probe(name: str, port: int, path: str, headers: dict[str, str] | None = None,
                    *, limit: int = 1024 * 1024) -> tuple[int | None, dict]:
        response = None
        try:
            async with application.state.poll_slots:
                probe_headers = {"Accept": "application/json", "X-Wrapper-Dashboard": "1"}
                probe_headers.update(headers or {})
                response = await application.state.http.get(
                    f"http://127.0.0.1:{port}{path}", allow_redirects=False,
                    headers=probe_headers,
                )
                raw = await bounded_read(response, limit)
                try:
                    parsed = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    parsed = {}
                return response.status, parsed if isinstance(parsed, (dict, list)) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, HTTPException):
            return None, {}
        finally:
            if response is not None:
                response.close()

    async def service_status(name: str, entry: dict,
                            headers: dict[str, str] | None = None) -> dict:
        port = entry["port"]
        results = await asyncio.gather(
            probe(name, port, "/health", headers),
            probe(name, port, "/ready", headers),
            probe(name, port, "/stats", headers),
            probe(name, port, "/v1/models?availability=all", headers, limit=2 * 1024 * 1024),
            probe(name, port, "/v1/capabilities?availability=all", headers, limit=2 * 1024 * 1024),
            probe(name, port, "/metrics/models", headers, limit=1024 * 1024),
            probe(name, port, "/metrics/activity?limit=50", headers, limit=512 * 1024),
            probe(name, port, "/metrics/chart/hourly?hours=24", headers, limit=512 * 1024),
        )
        ((health_code, health_body), (ready_code, ready_body),
         (stats_code, stats_body), (models_code, models_body),
         (capabilities_code, capabilities_body), (usage_code, usage_body),
         (activity_code, activity_body), (chart_code, chart_body)) = results
        metrics_code = None
        if stats_code == 404:
            metrics_code, metrics_body = await probe(name, port, "/metrics", headers)
            # Use metrics as a counter fallback, but retain the real /stats
            # status so endpoint gaps remain visible to the dashboard.
            stats_body = metrics_body
        reachable = health_code is not None
        ready = ready_body.get("ready") if isinstance(ready_body, dict) and ready_code == 200 else None
        ready = ready if isinstance(ready, bool) else None
        if health_code in (401, 403):
            status = "protected"
        elif not reachable:
            status = "offline"
        elif (health_code != 200
              or (isinstance(health_body, dict)
                  and health_body.get("status") in ("degraded", "error", "unhealthy"))
              or ready is False):
            status = "degraded"
        else:
            status = "online"
        qualification = entry.get("client_qualification", {})
        if not isinstance(qualification, dict):
            qualification = {}
        endpoint_status = {
            "health": health_code,
            "ready": ready_code,
            "stats": stats_code,
            "metrics": metrics_code,
            "models": models_code,
            "capabilities": capabilities_code,
            "usage": usage_code,
            "activity": activity_code,
            "chart_hourly": chart_code,
        }
        counter_values = {**safe_numbers(health_body), **safe_numbers(stats_body)}
        key_total = counter_values.get("keys", counter_values.get("total_keys", counter_values.get("keys_loaded")))
        key_available = counter_values.get("available", counter_values.get("available_keys"))
        key_summary = {
            "total": key_total if isinstance(key_total, (int, float)) else None,
            "available": key_available if isinstance(key_available, (int, float)) else None,
            "blocked": (key_total - key_available
                        if isinstance(key_total, (int, float))
                        and isinstance(key_available, (int, float))
                        else None),
            "labels_redacted": True,
        }
        policy = model_policy(name, entry, health_body, ready_body, stats_body)
        return {
            "name": name, "upstream": entry.get("upstream", name), "port": port,
            "lifecycle": "production", "status": status, "reachable": reachable,
            "health_http_status": health_code, "ready_http_status": ready_code, "ready": ready,
            "readiness": "ready" if ready is True else "not_ready" if ready is False else "protected" if ready_code in (401, 403) else "unknown",
            "counters": counter_values,
            "key_summary": key_summary,
            "free_only": policy["free_only"],
            "free_policy": policy["mode"],
            "model_policy": policy,
            "models_count": collection_count(models_body, "data", "models"),
            "capabilities_count": collection_count(capabilities_body, "models", "data"),
            "endpoint_status": endpoint_status,
            "auth_required": any(code in (401, 403) for code in endpoint_status.values()),
            "realtime": entry.get("dashboard_stream", "polling"),
            "service_kind": entry.get("service_kind"),
            "verified_model": qualification.get("verified_model"),
            "dashboard_available": False,
            "telemetry": {
                "health": public_json(health_body),
                "ready": public_json(ready_body),
                "stats": public_json(stats_body),
                "models": public_collection(models_body, "data", "models"),
                "capabilities": public_collection(capabilities_body, "models", "data"),
                "usage": public_json(usage_body),
                "activity": public_json(activity_body),
                "chart_hourly": public_json(chart_body),
            },
        }

    async def model_plane_snapshot(entries: dict,
                                   headers: dict[str, str] | None = None) -> dict:
        """Read the two model knowledge planes into the same live snapshot.

        The catalog/MCP service and the registry have different contracts and
        databases. The dashboard unifies their read view, but deliberately does
        not merge or write either store from the public dashboard process.
        """
        registry_port = int(entries.get("model-registry", {}).get("port", 9200))
        fetcher_results = await asyncio.gather(
            probe("model-fetcher-api", 8787, "/health", headers),
            probe("model-fetcher-api", 8787, "/ready", headers),
            probe("model-fetcher-api", 8787, "/stats", headers),
            probe("model-fetcher-api", 8787, "/providers", headers),
            probe("model-fetcher-api", 8787, "/models?limit=250", headers, limit=4 * 1024 * 1024),
            probe("model-fetcher-mcp", 9100, "/health", headers),
            probe("model-fetcher-mcp", 9100, "/ready", headers),
            probe("model-fetcher-mcp", 9100, "/mcp", headers),
            probe("model-registry", registry_port, "/health", headers),
            probe("model-registry", registry_port, "/ready", headers),
            probe("model-registry", registry_port, "/stats", headers),
        )
        (f_health_code, f_health), (f_ready_code, f_ready), (f_stats_code, f_stats), \
        (f_providers_code, f_providers), (f_models_code, f_models), \
        (mcp_health_code, mcp_health), (mcp_ready_code, mcp_ready), (mcp_protocol_code, mcp_protocol), \
        (r_health_code, r_health), (r_ready_code, r_ready), \
        (r_stats_code, r_stats) = fetcher_results

        def source_state(health_code, health_body, ready_code, ready_body):
            if health_code is None:
                return "offline"
            if health_code in (401, 403) or ready_code in (401, 403):
                return "protected"
            if health_code != 200 or (ready_code is not None and ready_code != 200):
                return "degraded"
            if isinstance(ready_body, dict) and ready_body.get("ready") is False:
                return "degraded"
            return "online"

        return {
            "stream_transport": "server_poll_to_sse",
            "refresh_interval_sec": 5,
            "model_fetcher": {
                "service": "model-fetcher-catalog-api",
                "port": 8787,
                "role": "auxiliary_metadata_only",
                "public_model_feed": False,
                "backend_uplink": False,
                "status": source_state(f_health_code, f_health, f_ready_code, f_ready),
                "endpoint_status": {
                    "health": f_health_code, "ready": f_ready_code,
                    "stats": f_stats_code, "providers": f_providers_code,
                    "models": f_models_code,
                },
                "health": public_json(f_health), "ready": public_json(f_ready),
                "stats": public_json(f_stats),
                "providers": public_collection(f_providers, "providers"),
                "models": public_collection(f_models, "models", "data"),
                "models_count": collection_count(f_models, "models", "data"),
                "dashboard_available": False,
                "note": "Reference metadata only; live wrapper /v1/models remains the public model feed and inference backend.",
            },
            "model_fetcher_mcp": {
                "service": "model-fetcher-mcp",
                "port": 9100,
                "status": source_state(mcp_health_code, mcp_health, mcp_ready_code, mcp_ready),
                "endpoint_status": {"health": mcp_health_code, "ready": mcp_ready_code, "mcp": mcp_protocol_code},
                "health": public_json(mcp_health), "ready": public_json(mcp_ready),
                "protocol": {"http_status": mcp_protocol_code, "transport": "streamable_http"},
                "dashboard_available": False,
                "note": "MCP transport remains available; its local dashboard is disabled.",
            },
            "model_registry": {
                "service": "model-registry",
                "port": registry_port,
                "status": source_state(r_health_code, r_health, r_ready_code, r_ready),
                "endpoint_status": {"health": r_health_code, "ready": r_ready_code, "stats": r_stats_code},
                "health": public_json(r_health), "ready": public_json(r_ready),
                "stats": public_json(r_stats),
                "providers": {},
                "models_count": 0,
                "dashboard_available": False,
                "model_feed": "excluded",
                "note": "Control-plane dependency only; provider model rows are excluded from the dashboard feed. Runtime wrappers own their live model metadata and call plans.",
            },
        }

    async def build_status(entries: dict, active: tuple[str, ...],
                           headers: dict[str, str] | None = None) -> dict:
        """Probe wrappers without holding the cache mutex during network I/O."""
        statuses, model_planes = await asyncio.gather(
            asyncio.gather(*(service_status(name, entries[name], headers) for name in active)),
            model_plane_snapshot(entries, headers),
        )
        statuses = list(statuses)
        pending = sorted((
            {
                "name": name, "upstream": entry.get("upstream", name), "port": entry.get("port"),
                "lifecycle": entry["status"], "status": "pending", "reachable": None,
                "ready": None, "readiness": "not_probed", "counters": {},
                "models_count": None, "capabilities_count": None,
                "free_only": None, "free_policy": "not_probed",
                "endpoint_status": {}, "auth_required": False,
                "realtime": entry.get("dashboard_stream", "polling"),
                "service_kind": entry.get("service_kind"),
                "dashboard_available": False,
            }
            for name, entry in entries.items()
            if name not in active and name != "dashboard"
            and entry.get("service_kind") != "dashboard"
        ), key=lambda item: (item["port"] if isinstance(item["port"], int) else 65536, item["name"]))
        statuses.extend(pending)
        inference_names = tuple(
            name for name in active if entries[name].get("service_kind") != "internal"
        )
        internal_names = tuple(name for name in active if name not in inference_names)
        inference_active = len(inference_names)
        internal_active = len(internal_names)
        return {
            "checked_at": int(time.time()),
            "read_only": True,
            "stream": {
                "transport": "sse",
                "source": "bounded server-side snapshots",
                "interval_sec": 5,
                "authenticated": bool(headers),
            },
            "wrappers": statuses,
            "model_planes": model_planes,
            "counts": {
                "active": len(active),
                "inference_active": inference_active,
                "internal_active": internal_active,
                "online": sum(s["status"] == "online" for s in statuses),
                "ready": sum(s["ready"] is True for s in statuses),
                "inference_online": sum(
                    s["status"] == "online" for s in statuses if s["name"] in inference_names
                ),
                "inference_ready": sum(
                    s["ready"] is True for s in statuses if s["name"] in inference_names
                ),
                "internal_online": sum(
                    s["status"] == "online" for s in statuses if s["name"] in internal_names
                ),
                "pending": sum(s["status"] == "pending" for s in statuses),
            },
        }

    async def collect_status(headers: dict[str, str] | None = None) -> dict:
        """Return a short-lived snapshot with a per-credential single flight.

        Cache reads/writes are serialized, but slow wrapper probes run outside
        the mutex. Concurrent callers for the same metadata/auth scope share
        one refresh task; a slow or offline provider therefore cannot serialize
        unrelated dashboard requests or create a probe stampede.
        """
        entries, active = metadata(repo)
        signature = json.dumps(entries, sort_keys=True)
        scope = auth_scope(headers or {})
        key = (signature, scope)
        async with application.state.cache_lock:
            cached = application.state.cached
            if (cached and cached[0] == signature and cached[1] == scope
                    and time.monotonic() - cached[2] < 5):
                return cached[3]
            task = application.state.refresh_tasks.get(key)
            if task is None:
                task = asyncio.create_task(build_status(entries, active, headers))
                application.state.refresh_tasks[key] = task
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Shield leaves the shared refresh alive for other callers.
            raise
        except BaseException:
            async with application.state.cache_lock:
                if application.state.refresh_tasks.get(key) is task:
                    application.state.refresh_tasks.pop(key, None)
            raise
        async with application.state.cache_lock:
            if application.state.refresh_tasks.get(key) is task:
                application.state.refresh_tasks.pop(key, None)
                application.state.cached = signature, scope, time.monotonic(), result
        return result

    @application.get("/api/status")
    async def status(request: Request):
        return await collect_status(forwarded_auth(request))

    @application.get("/api/status/stream")
    async def status_stream(request: Request):
        """Push safe, aggregated wrapper status snapshots over SSE.

        The stream deliberately uses public health/discovery surfaces and does
        not accept credentials in the query string. If a deployment protects
        those surfaces, the normal /api/status polling path can still forward
        a browser-supplied Authorization/x-api-key header without leaking it
        into an EventSource URL.
        """
        async def stream():
            yield b"retry: 3000\n\n"
            try:
                while True:
                    try:
                        disconnected = await request.is_disconnected()
                    except (KeyError, RuntimeError):
                        # Direct ASGI/unit callers may not provide a receive
                        # channel; the production server does.
                        disconnected = False
                    if disconnected:
                        return
                    payload = await collect_status(forwarded_auth(request))
                    yield ("event: status\ndata: " + json.dumps(
                        payload, separators=(",", ":"), ensure_ascii=False
                    ) + "\n\n").encode("utf-8")
                    # A short sleep gives clients a genuinely live channel while
                    # avoiding a busy loop when a provider is unavailable.
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not expose upstream details or secrets in the SSE stream.
                yield b'event: error\ndata: {"message":"status stream unavailable"}\n\n'

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @application.get("/wrappers/{name}/{path:path}")
    async def proxy(name: str, path: str, request: Request):
        entries, active = metadata(repo)
        if name not in entries:
            raise HTTPException(404, "Unknown wrapper")
        if name not in active:
            raise HTTPException(409, "Pending wrapper is frozen; dashboard access is disabled")
        upstream_path = "/" + path
        if upstream_path in ("/dashboard", "/dashboard.html"):
            raise HTTPException(410, "Per-wrapper dashboards are disabled; use the unified dashboard")
        if upstream_path not in PROXY_PATHS:
            raise HTTPException(404, "Dashboard proxy permits only selected read-only endpoints")
        query = urlencode([(key, value) for key, value in request.query_params.multi_items()
                           if not SECRET_QUERY.search(key)])
        url = f"http://127.0.0.1:{entries[name]['port']}{upstream_path}" + ("?" + query if query else "")
        forwarded = {key: value for key, value in request.headers.items()
                     if key.lower() in ("authorization", "x-api-key", "accept", "last-event-id")}
        slots = application.state.stream_slots if upstream_path == "/events" else application.state.slots
        try:
            await asyncio.wait_for(slots.acquire(), timeout=2)
        except asyncio.TimeoutError as exc:
            raise HTTPException(503, "Dashboard connection capacity reached; retry shortly") from exc
        upstream = None
        closed = False

        def cleanup():
            nonlocal closed
            if not closed:
                closed = True
                if upstream is not None:
                    upstream.close()
                slots.release()

        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=1.5, sock_read=45) if upstream_path == "/events" else aiohttp.ClientTimeout(total=8, connect=1.5, sock_read=5)
            upstream = await application.state.http.get(url, headers=forwarded,
                                                       allow_redirects=False, timeout=timeout)
            if 300 <= upstream.status < 400:
                raise HTTPException(502, "Wrapper redirect blocked by dashboard")
            content_type = upstream.headers.get("Content-Type", "application/octet-stream")
            if upstream_path == "/events" and upstream.status == 200 and "text/event-stream" in content_type:
                async def stream():
                    try:
                        async for chunk in upstream.content.iter_chunked(65536):
                            yield chunk
                    finally:
                        cleanup()
                return StreamingResponse(stream(), status_code=upstream.status,
                                         headers={"Content-Type": content_type, "X-Accel-Buffering": "no"},
                                         background=BackgroundTask(cleanup), on_close=cleanup)
            body = await bounded_read(upstream)
            status_code = upstream.status
            if upstream_path in ("/dashboard", "/dashboard.html") and status_code == 200 and "text/html" in content_type:
                body = bridge_html(body.decode("utf-8", errors="replace"), name).encode()
            cleanup()
            return Response(body, status_code=status_code, headers={"Content-Type": content_type})
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            cleanup()
            raise HTTPException(502, "Wrapper is unavailable or timed out") from exc
        except BaseException:
            cleanup()
            raise

    return application


app = create_app()
