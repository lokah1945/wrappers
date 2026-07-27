#!/usr/bin/env python3
"""
wrapper-nous v2.0.1 — PRODUCTION-GRADE (FastAPI + async) + Hermes/Codex/Claude Code fixes
Standard OpenAI + Anthropic + Responses compatible proxy for Nous Research.

Production-hardened (audited 2026-07-27):
- Async FastAPI + Uvicorn
- Full streaming with proxy-side heartbeat (anti-silence)
- Proper Responses API streaming (event: response.created / output_text.delta / completed)
- Parallel tool calls streaming (Anthropic + Responses)
- Correct handling for name:null tools (Codex compatibility)
- Thinking / reasoning injection passthrough
- Full OpenAI + Anthropic SDK compatibility
- Metrics (JSON + Prometheus)
- Rich model metadata + capabilities + aliases for Claude Code
- Rate limiting + error normalization
- anthropic-beta / openai-beta passthrough

Upstream: https://inference-api.nousresearch.com/v1/chat/completions
"""

import os
import json
import time
import copy
import random
import asyncio
import threading
import logging
from typing import Optional, Dict, List, AsyncGenerator, Set
from contextlib import asynccontextmanager
from pathlib import Path
import sys

# Shared persistent catalog/state layer; bootstrap repo root for systemd launches.
try:
    from common.model_state import ModelStateStore, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id, classify_upstream_error
except ImportError:
    # Audit/transparency tooling may load a temporary copy of this file; the
    # monorepo root is still the current working directory in that mode.
    for _root in (Path.cwd(), Path(__file__).resolve().parents[1], Path(__file__).resolve().parents[2]):
        if (_root / 'common').is_dir():
            sys.path.insert(0, str(_root))
            break
    from common.model_state import ModelStateStore, credential_fingerprint
    from common.model import LocalModelRegistry, ModelRegistryClient, same_provider_model_id, classify_upstream_error

import aiohttp

# Circuit breaker for upstream protection
try:
    from common.circuit_breaker import CircuitBreaker, CircuitBreakerError
    _UPSTREAM_BREAKER = CircuitBreaker(failure_threshold=10, recovery_timeout=30, name="nous-upstream")
    _HAS_CIRCUIT_BREAKER = True
except ImportError:
    _HAS_CIRCUIT_BREAKER = False

# Shared header sanitization (BUG-SEC2 fix — deduplicated from common/middleware)
try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../nous only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.middleware import sanitize_header_value as _sanitize_header_value
except ImportError:
    # Fallback sanitizer: upstream common.middleware is missing from the
    # repo, so provide the BUG-SEC2 header-injection guard inline.
    def _sanitize_header_value(value):
        if not isinstance(value, str):
            value = str(value)
        import re as _re
        return _re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value).strip()


# Shared translation utilities from common/translations (deduplication).
# Note: Nous uses a dict-based AnthropicStreamState (for stream_with_heartbeat),
# so we only import the string-agnostic utilities here.
try:
    from common.translations import (
        parse_dsml_from_text as _parse_dsml_from_text,
        repair_orphan_tool_messages as _repair_orphan_tool_messages,
        normalize_upstream_error as _normalize_upstream_error,
        strip_cache_control as strip_cache_control,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError:
    _USING_SHARED_TRANSLATIONS = False


# ============================================================================
# KeyPool for multi-key rotation (parity with opencode/nvidia-python)
# ============================================================================

def validate_config():
    """Validate required configuration at startup."""
    import os
    import sys
    
    missing = []
    for var in ['NOUS_API_KEY_1', 'BEARER_TOKEN']:
        if not os.environ.get(var):
            missing.append(var)
    
    if missing:
        print(f"❌ ERROR: Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)
    
    # Validate port range
    try:
        port = int(os.environ.get('LISTEN_PORT', '9102'))
        if not (1024 <= port <= 65535):
            print(f"❌ ERROR: Invalid port {port}")
            sys.exit(1)
    except ValueError:
        print(f"❌ ERROR: LISTEN_PORT must be an integer")
        sys.exit(1)


class AnthropicStreamState:
    def __init__(self, model):
        self.model = model
        self.index = -1  # first content block must be index 0 (Anthropic SDK)
        self.message_started = False
        self.current_block = None
        self.tool_map = {}
        self.finished = False
        self.msg_id = f"msg-{int(time.time()*1000)}"

    def _usage(self, raw=None):
        raw = raw or {}
        return {
            "input_tokens": raw.get("prompt_tokens", raw.get("input_tokens", 0)) or 0,
            "output_tokens": raw.get("completion_tokens", raw.get("output_tokens", 0)) or 0,
            "cache_creation_input_tokens": raw.get("cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": raw.get("cache_read_input_tokens", 0) or 0,
        }

    def translate_chunk(self, chunk):
        events = []
        if not self.message_started:
            events.append({"type": "message_start", "data": {"type": "message_start", "message": {
                "id": self.msg_id, "type": "message", "role": "assistant", "model": self.model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": self._usage(),
            }}})
            self.message_started = True

        if "choices" not in chunk:
            return events
        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta", {}) or {}

        # reasoning / thinking delta
        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            if self.current_block != "thinking":
                if self.current_block:
                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})
                self.index += 1
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "thinking", "thinking": ""},
                }})
                self.current_block = "thinking"
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "thinking_delta", "thinking": reason},
            }})

        # If content looks like DSML tool markup, do NOT emit as text_delta (prevent leak)
        content = delta.get("content")
        if isinstance(content, str) and content and "DSML" in content.replace("\uff5c", "|"):
            # skip raw DSML text; structured tool_calls path should carry tools
            content = None

        if content:
            if self.current_block != "text":
                if self.current_block:
                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})
                self.index += 1
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "text", "text": ""},
                }})
                self.current_block = "text"
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "text_delta", "text": content},
            }})

        for tc in delta.get("tool_calls", []) or []:
            idx = tc.get("index", 0)
            fn = tc.get("function", {}) or {}
            if idx not in self.tool_map:
                if self.current_block:
                    events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})
                self.index += 1
                self.tool_map[idx] = self.index
                tid = tc.get("id") or f"toolu_{self.index}"
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "tool_use", "id": tid, "name": fn.get("name", "") or "", "input": {}},
                }})
                self.current_block = "tool_use"
            tidx = self.tool_map[idx]
            if "arguments" in fn and fn.get("arguments") is not None:
                events.append({"type": "content_block_delta", "data": {
                    "type": "content_block_delta", "index": tidx,
                    "delta": {"type": "input_json_delta", "partial_json": fn.get("arguments") or ""},
                }})

        if ch.get("finish_reason") and not self.finished:
            self.finished = True
            if self.current_block is not None:
                events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})
            fr = ch.get("finish_reason")
            stop = "tool_use" if (fr == "tool_calls" or self.tool_map) else (
                {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}.get(fr, "end_turn")
            )
            events.append({"type": "message_delta", "data": {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": self._usage(chunk.get("usage") or {}),
            }})
            events.append({"type": "message_stop", "data": {"type": "message_stop"}})
            self.current_block = None
        return events

    def done(self):
        """Emit terminal events if stream ended without finish_reason (prevent hang)."""
        if self.finished:
            return []
        events = []
        if not self.message_started:
            self.message_started = True
            events.append({"type": "message_start", "data": {"type": "message_start", "message": {
                "id": self.msg_id, "type": "message", "role": "assistant", "model": self.model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": self._usage(),
            }}})
        if self.current_block is not None:
            events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": self.index}})
            self.current_block = None
        stop = "tool_use" if self.tool_map else "end_turn"
        events.append({"type": "message_delta", "data": {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": self._usage(),
        }})
        events.append({"type": "message_stop", "data": {"type": "message_stop"}})
        self.finished = True
        return events


class ResponsesStreamState:
    """Full Responses streaming state (for Codex / Claude Code / OpenAI Responses SDK)"""
    def __init__(self, rid, model):
        self.rid = rid
        self.model = model
        self.seq = 0
        self.text_idx = 1
        self.tool_acc = {}
        self._next_tool_index = 1
        self.reasoning_started = False
        self.started = False
        self._active_tool_id = None
        self._completed = False
        self._finished = False
        self.accum_usage = {}

    def next_seq(self):
        self.seq += 1
        return self.seq

    def emit(self, etype, data):
        payload = {"type": etype, "sequence_number": self.next_seq(), **data}
        return f"event: {etype}\ndata: {json.dumps(payload)}\n\n"

    def start(self):
        if self.started:
            return []
        self.started = True
        rid = self.rid
        # OpenAI Responses API requires the output item to be "added" (made active)
        # BEFORE any output_text.delta is sent, otherwise clients like Codex v0.145
        # emit "OutputTextDelta without active item" and hang.
        return [
            self.emit("response.created", {"response": {"id": rid, "model": self.model, "status": "in_progress"}}),
            self.emit("response.in_progress", {"response": {"id": rid, "status": "in_progress"}}),
            self.emit("response.output_item.added", {
                "output_index": 0,
                "item": {"id": "msg-1", "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []},
            }),
            self.emit("response.content_part.added", {
                "item_id": "msg-1", "output_index": 0, "content_index": 0,
                "part": {"type": "output_text", "text": ""},
            }),
        ]

    def delta(self, text):
        self.final_text = getattr(self, "final_text", "") + text
        return self.emit("response.output_text.delta", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": text})

    def tool_delta(self, call_id, name, args):
        events = []
        if call_id not in self.tool_acc:
            self.tool_acc[call_id] = {"name": name, "args": "", "output_index": self._next_tool_index}
            self._next_tool_index += 1
            # Make the tool item active BEFORE sending its delta (Codex requires this).
            events.append(self.emit("response.output_item.added", {
                "output_index": self.tool_acc[call_id]["output_index"],
                "item": {
                    "id": call_id, "type": "function_call", "status": "in_progress",
                    "call_id": call_id, "name": name, "arguments": "",
                },
            }))
        self.tool_acc[call_id]["name"] = self.tool_acc[call_id]["name"] or name
        self.tool_acc[call_id]["args"] += args
        events.append(self.emit("response.function_call.delta", {
            "item_id": call_id, "output_index": self.tool_acc[call_id]["output_index"], "delta": args,
        }))
        return events

    def _normalize_usage(self, u):
        if not u:
            u = self.accum_usage or {}
        else:
            self.accum_usage.update(u)
        prompt = u.get("prompt_tokens") or u.get("input_tokens") or 0
        completion = u.get("completion_tokens") or u.get("output_tokens") or 0
        # OpenAI Responses API schema requires total_tokens alongside input/output.
        return {
            "input_tokens": int(prompt),
            "output_tokens": int(completion),
            "total_tokens": int(prompt) + int(completion),
        }

    def done(self, usage=None):
        # MUST return a list — stream_with_heartbeat iterates this.
        # Idempotent: emit response.completed exactly once.
        if self._completed:
            return []
        self._completed = True
        norm = self._normalize_usage(usage)
        rid = self.rid
        text = getattr(self, "final_text", "")
        events = [
            self.emit("response.output_text.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "text": text}),
            self.emit("response.content_part.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text}}),
            self.emit("response.output_item.done", {"output_index": 0, "item": {"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}),
        ]
        # Close every tool item that was opened (Codex hangs if a function_call
        # item is added but never marked done).
        for call_id, info in self.tool_acc.items():
            events.append(self.emit("response.output_item.done", {
                "output_index": info.get("output_index", 1),
                "item": {
                    "id": call_id, "type": "function_call", "status": "completed",
                    "call_id": call_id, "name": info.get("name", ""),
                    "arguments": info.get("args", ""),
                },
            }))
        output = [{"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
        for call_id, info in self.tool_acc.items():
            output.append({"id": call_id, "type": "function_call", "status": "completed", "call_id": call_id, "name": info.get("name", ""), "arguments": info.get("args", "")})
        events.append(self.emit("response.completed", {"response": {"id": rid, "model": self.model, "status": "completed", "output": output, "usage": norm}}))
        return events

    def assistant_message(self):
        text = getattr(self, "final_text", "")
        msg = {"role": "assistant", "content": text or (None if self.tool_acc else "")}
        if self.tool_acc:
            msg["tool_calls"] = [
                {"id": call_id, "type": "function", "function": {"name": info.get("name", ""), "arguments": info.get("args", "")}}
                for call_id, info in self.tool_acc.items()
            ]
        return msg

    def translate_chunk(self, chunk):
        """Convert OpenAI chat chunk → Responses events"""
        events = []
        if not self.started:
            events.extend(self.start())

        # Accumulate usage from any chunk (Nous sends it separately from finish_reason)
        if isinstance(chunk, dict) and chunk.get("usage"):
            self.accum_usage.update(chunk["usage"])

        if "choices" not in chunk:
            return events

        ch = chunk["choices"][0]
        delta = ch.get("delta", {})

        # Text
        if delta.get("content"):
            events.append(self.delta(delta["content"]))

        # Tool calls (parallel support)
        for tc in delta.get("tool_calls", []):
            fn = tc.get("function", {})
            raw_id = tc.get("id")
            if raw_id:
                self._active_tool_id = raw_id
            call_id = self._active_tool_id or (tc.get("id") or f"call_{len(self.tool_acc)}")
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            if name or args:
                events.extend(self.tool_delta(call_id, name, args))

        # Completion event is emitted exactly once at [DONE] in stream_with_heartbeat.
        # This avoids a double response.completed (one with empty usage) that breaks
        # OpenAI Responses SDK / Codex parsing ("missing field input_tokens").
        if ch.get("finish_reason"):
            self._finished = True
        return events

# --------------------------------------------------------------------------
# METRICS
# --------------------------------------------------------------------------


async def record_model_result(model_id: str, key_entry, status: int, payload, endpoint: str) -> None:
    """Persist account-scoped upstream outcome without hard-blocking models."""
    try:
        credential = getattr(key_entry, "api_key", None)
        if status == 200:
            stored = await MODEL_STORE.record_status_async(
                model_id=model_id or "unknown",
                account_scope=credential_fingerprint(credential),
                state="available",
                status_code=status,
                reason_code="OK",
                endpoint=endpoint,
            )
        else:
            stored = await MODEL_STORE.record_error_async(model_id or "unknown", credential, status, payload, endpoint)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            "nous", model_id or "unknown", stored.get("account_scope", "unknown"),
            stored.get("state", "unknown"), status, stored.get("reason_code", ""),
            stored.get("reason_detail", ""), endpoint,
        )
    except Exception as e:
        logger.warning(f"[model-state] Nous result record failed: {e}")


# N-07/F3 round-2 fix: the event loop keeps only WEAK references to tasks, so
# a bare create_task with no retained handle can be garbage-collected
# mid-flight (silently dropping e.g. Codex previous_response_id history or a
# model-state write). Keep a strong reference until the task completes.
_BG_TASKS: set = set()


def _fire_and_forget(coro, label: str = "bg") -> None:
    """Schedule a background task with a retained reference and exception
    logging (N-07/F3 fix: _BG_TASKS pattern, nvidia parity)."""
    def _done(task: "asyncio.Task"):
        _BG_TASKS.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.warning(f"[{label}] background task failed: {exc}")
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        # No running event loop (import-time/test context) — close the coroutine.
        coro.close()
        return
    _BG_TASKS.add(task)
    task.add_done_callback(_done)

# --------------------------------------------------------------------------
# RATE LIMIT
# --------------------------------------------------------------------------
from collections import defaultdict
from .key_pool import KeyPool
from .metrics import metrics
rate_limits = defaultdict(list)
_rate_limit_lock = threading.Lock()

def check_rate_limit(ip: str):
    now = time.time()
    with _rate_limit_lock:
        rate_limits[ip] = [t for t in rate_limits[ip] if now - t < 60]
        if len(rate_limits[ip]) >= RATE_LIMIT_RPM:
            return False
        rate_limits[ip].append(now)
        # N-14 round-2 fix: prune keys whose timestamps are ALL older than the
        # 60s TTL (not just already-empty lists) — a one-shot client IP
        # previously kept its stale non-empty list forever, so the defaultdict
        # grew unboundedly over long uptimes with many distinct addresses.
        stale = [k for k, v in rate_limits.items()
                 if k != ip and (not v or all(now - t >= 60 for t in v))]
        for k in stale:
            del rate_limits[k]
    return True


# N-11 fix (BUG-SEC3 sibling-gap): shared max-tokens validation used by all
# three inference endpoints, not only /v1/chat/completions.
MAX_TOKENS_CAP = 1000000

def validate_max_tokens_field(value, field_name: str, required: bool = False):
    """Return an error message (str) or None if the field is acceptable."""
    if value is None:
        if required:
            return f"{field_name} is required and must be a positive integer"
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        # N-11 fix: type guard — int("abc")-style crashes previously produced 500s.
        return f"{field_name} must be a positive integer"
    if value <= 0:
        return f"{field_name} must be a positive integer"
    if value > MAX_TOKENS_CAP:
        return f"{field_name} exceeds maximum allowed value of {MAX_TOKENS_CAP}"
    return None


async def refresh_model_catalog_once():
    """Refresh the persistent Nous catalog without touching inference traffic."""
    try:
        status, data = await get_nous_json_with_retries("/v1/models")
        models_data = data.get("data", []) if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source="nous:/v1/models")
            MODEL_REGISTRY.register_catalog(models_data, revision="runtime-catalog")
            MODEL_REGISTRY_CLIENT.schedule_catalog("nous", models_data, "runtime-catalog")
            logger.info(f"[model-catalog] Nous refreshed {len(models_data)} models")
    except Exception as e:
        logger.warning(f"[model-catalog] Nous refresh failed: {e}")


async def model_catalog_refresh_loop():
    while True:
        await asyncio.sleep(max(60, MODEL_CATALOG_REFRESH_SEC))
        await refresh_model_catalog_once()

# --------------------------------------------------------------------------
# FASTAPI APP
# --------------------------------------------------------------------------
async def _heal_in_flight_loop():
    """N-01 fix: periodic heal of leaked in_flight slots (nvidia parity)."""
    interval = int(os.environ.get("HEAL_INFLIGHT_INTERVAL_SEC", "300"))
    while True:
        await asyncio.sleep(max(30, interval))
        try:
            KEY_POOL.heal_in_flight()
        except Exception as e:
            logger.warning(f"[key_pool] heal_in_flight loop error: {e}")


_HEAL_TASK = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _MODEL_REFRESH_TASK, _HEAL_TASK, _known_models
    seed = (os.environ.get("DYNAMIC_ALIAS_TARGET") or "").strip()
    if seed and not is_alias_name(seed):
        set_dynamic_alias_target(seed, force=True)
        MODEL_REGISTRY.bind_explicit_aliases(seed, _ALIAS_NAME_SET, scope_type="wrapper", scope_id="nous")
    logger.info(f"wrapper-nous v{VERSION} starting on {LISTEN_HOST}:{LISTEN_PORT}")
    start_env_watcher()
    # Load API keys from environment before the daily catalog task starts.
    KEY_POOL.load_from_env()
    # N-17 fix: populate _known_models at startup from the persisted catalog so
    # dynamic alias binding works before the first /v1/models discovery call.
    try:
        boot_known: Set[str] = set()
        for m in (MODEL_STORE.get_catalog(fresh_only=False) or []):
            mid = m.get("id") if isinstance(m, dict) else None
            if mid:
                boot_known.add(mid)
        for m in CURATED_FREE_MODELS:
            boot_known.add(m["id"])
        _known_models = boot_known
        logger.info(f"[models] seeded {len(_known_models)} known model ids from persisted catalog")
    except Exception as e:
        logger.warning(f"[models] startup catalog seed failed: {e}")
    await MODEL_REGISTRY_CLIENT.start()
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    _HEAL_TASK = asyncio.create_task(_heal_in_flight_loop())
    yield

    # Graceful shutdown: wait for in-flight requests
    logger.info(f"[{wrapper_name}] Starting graceful shutdown...")
    shutdown_start = time.time()
    max_wait = 30
    while shutdown_start + max_wait > time.time():
        total = sum(k.in_flight for k in KEY_POOL.keys)
        if total == 0:
            logger.info(f"[nous] All requests drained")
            break
        await asyncio.sleep(0.1)
    logger.info('[lifecycle] wrapper-nous shutting down gracefully...')
    for _task in (_MODEL_REFRESH_TASK, _HEAL_TASK):
        if _task:
            _task.cancel()
            try:
                await _task
            except asyncio.CancelledError:
                pass
    _MODEL_REFRESH_TASK = None
    _HEAL_TASK = None
    await MODEL_REGISTRY_CLIENT.stop()
    # Cleanup: close aiohttp session
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        try:
            await _SESSION.close()
        except Exception:
            pass
    logger.info("Shutdown complete")

app = FastAPI(title="wrapper-nous", version=VERSION, lifespan=lifespan)


# Request latency tracking middleware
@app.middleware("http")
async def add_latency_tracking(request: Request, call_next):
    import time
    start_time = time.time()
    
    response = await call_next(request)
    
    latency_ms = (time.time() - start_time) * 1000
    request_id = request.headers.get("x-request-id", "N/A")
    
    logger.info(
        f"[{app.title}] request_id={request_id} "
        f"method={request.method} path={request.url.path} "
        f"latency={latency_ms:.2f}ms status={response.status_code}"
    )
    
    response.headers["X-Process-Time"] = f"{latency_ms:.2f}ms"
    return response


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and ("error" in detail or detail.get("type") == "error"):
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": {"type": "api_error", "message": str(detail)}})
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r'https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$',
    allow_methods=['*'],
    allow_headers=['*'],
    expose_headers=['*'],
    allow_credentials=True,
)

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter)

async def _auth_check(request: Request):
    if request.method == 'OPTIONS':
        return  # CORS preflight passes without auth
    if not BEARER_TOKEN: return
    auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if token != BEARER_TOKEN:
        raise HTTPException(401, detail={"error": {"type": "authentication_error", "message": "Unauthorized"}})

@app.get("/health")
async def health():
    return {
        "ok": True,
        "status": "ok" if KEY_POOL.available_keys > 0 or bool(_read_token_from_auth_path()) else "degraded",
        "version": VERSION,
        "git_commit": GIT_COMMIT,
        "source_root": SOURCE_ROOT,
        "pid": os.getpid(),
        "port": LISTEN_PORT,
        "free_only": free_only_enabled(),
        "dynamic_alias_target": get_dynamic_alias_target() or None,
        "keys": KEY_POOL.total_keys,
        "available": KEY_POOL.available_keys,
        "live_keys": KEY_POOL.all_stats(),
        "metrics": metrics.snapshot(),
        "model_registry": MODEL_REGISTRY_CLIENT.stats(),
    }


# N-16 fix: /ready must not hit upstream (through the key pool, burning RPM)
# on every probe. Serve readiness from the cached catalog age and rate-limit
# live probes to at most one per READY_PROBE_MIN_INTERVAL_SEC.
READY_PROBE_MIN_INTERVAL_SEC = int(os.environ.get("READY_PROBE_MIN_INTERVAL_SEC", "60"))
_ready_probe_state = {"last_probe": 0.0, "status": None, "error": None}

@app.get("/ready")
async def ready():
    """Readiness checks credentials and catalog reachability, not a hidden model."""
    try:
        has_credentials = KEY_POOL.total_keys > 0 or bool(_read_token_from_auth_path())
        # Fresh cached catalog → upstream was reachable recently; no live call.
        if MODEL_STORE.get_catalog(fresh_only=True):
            return {
                "ready": has_credentials,
                "upstream_ok": True,
                "status_code": 200,
                "source": "catalog_cache",
                "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
                "last_error": None,
                "keys": KEY_POOL.total_keys,
                "available": KEY_POOL.available_keys,
            }
        now = time.time()
        if now - _ready_probe_state["last_probe"] >= READY_PROBE_MIN_INTERVAL_SEC or _ready_probe_state["status"] is None:
            _ready_probe_state["last_probe"] = now
            status, result = await get_nous_json_with_retries("/v1/models")
            _ready_probe_state["status"] = status
            _ready_probe_state["error"] = None if status == 200 else (result.get("error") if isinstance(result, dict) else str(result))
        status = _ready_probe_state["status"]
        return {
            "ready": status == 200 and has_credentials,
            "upstream_ok": status == 200,
            "status_code": status,
            "source": "live_probe_cached",
            "last_error": _ready_probe_state["error"],
            "keys": KEY_POOL.total_keys,
            "available": KEY_POOL.available_keys,
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"ready": False, "upstream_ok": False, "last_error": str(e), "keys": KEY_POOL.total_keys, "available": KEY_POOL.available_keys})

@app.get("/version")
async def version(): return {"version": VERSION, "git_commit": GIT_COMMIT, "source_root": SOURCE_ROOT, "pid": os.getpid()}

# Curated discovery manifest for stale/upstream-unavailable catalog responses
CURATED_FREE_MODELS = [
    {"id": "tencent/hy3:free", "object": "model", "owned_by": "nous", "context_window": 128000, "max_tokens": 4096, "supports_tools": True},
    {"id": "poolside/laguna-s-2.1:free", "object": "model", "owned_by": "nous", "context_window": 1048576, "max_tokens": 131072, "supports_tools": True},
    {"id": "big-pickle", "object": "model", "owned_by": "nous", "context_window": 128000, "max_tokens": 32768, "supports_tools": True},
]

_SESSION = None
_SESSION_LOCK: Optional[asyncio.Lock] = None

def _get_session_lock() -> asyncio.Lock:
    """Lazy-init the session lock on the running event loop."""
    global _SESSION_LOCK
    if _SESSION_LOCK is None:
        _SESSION_LOCK = asyncio.Lock()
    return _SESSION_LOCK

# F8 round-2 fix: the AUTH_PATH token file was opened and JSON-parsed on every
# request. Cache the token in memory; refresh when the file mtime changes or
# after an upstream auth failure (_invalidate_auth_token_cache()).
_AUTH_TOKEN_CACHE = {"token": None, "mtime": None}


def _invalidate_auth_token_cache():
    """Force the next _read_token_from_auth_path() to re-read AUTH_PATH
    (called when upstream rejects the cached OAuth token)."""
    _AUTH_TOKEN_CACHE["token"] = None
    _AUTH_TOKEN_CACHE["mtime"] = None


def _read_token_from_auth_path():
    """Read OAuth access token from AUTH_PATH (Hermes profile format).

    F8 fix: cached in memory; re-read only on file-mtime change or after
    _invalidate_auth_token_cache() (auth failure).
    """
    if not AUTH_PATH or not os.path.exists(AUTH_PATH):
        return None
    try:
        mtime = os.path.getmtime(AUTH_PATH)
        if _AUTH_TOKEN_CACHE["token"] is not None and _AUTH_TOKEN_CACHE["mtime"] == mtime:
            return _AUTH_TOKEN_CACHE["token"]
        with open(AUTH_PATH) as f:
            data = json.load(f)
        # Extract token from hermes profile format
        token = data.get("providers", {}).get("nous", {}).get("access_token")
        token = token if token else None
        _AUTH_TOKEN_CACHE["token"] = token
        _AUTH_TOKEN_CACHE["mtime"] = mtime
        return token
    except Exception as e:
        logger.warning(f"[auth] Failed to read token from AUTH_PATH: {e}")
        return None

async def get_token():
    """Get Nous API token: prefer AUTH_PATH (OAuth), fallback to KEY_POOL."""
    # Priority 1: OAuth token from AUTH_PATH
    token = _read_token_from_auth_path()
    if token:
        return token
    # Priority 2: Use KeyPool (NOUS_API_KEY, NOUS_API_KEY_1, etc.)
    entry = KEY_POOL.peek()
    if entry:
        return entry.api_key
    logger.warning("[auth] No API key configured! Set NOUS_API_KEY* or AUTH_PATH.")
    return ""

async def get_session():
    """Reuse one aiohttp session with lock protection (BUG-H1 fix)."""
    global _SESSION
    lock = _get_session_lock()
    async with lock:
        if _SESSION is None or _SESSION.closed:
            # N-06 fix: session default covers non-streaming calls; streaming
            # requests override with total=None + sock_read in post_nous.
            _SESSION = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC, sock_connect=CONNECT_TIMEOUT_SEC),
                connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS_PER_HOST, ttl_dns_cache=300, enable_cleanup_closed=True),
            )
        return _SESSION

@app.get("/v1/models")
async def models():
    # Use a persistent stale-while-revalidate catalog.  Discovery must not
    # depend on a live upstream call on every client request.
    upstream_models = MODEL_STORE.get_catalog(fresh_only=True)
    if not upstream_models:
        try:
            status, data = await get_nous_json_with_retries("/v1/models")
            if status == 200 and isinstance(data, dict):
                upstream_models = data.get("data", [])
                if upstream_models:
                    MODEL_STORE.upsert_catalog(upstream_models, source="nous:/v1/models")
                    MODEL_REGISTRY.register_catalog(upstream_models, revision="runtime-catalog")
                    MODEL_REGISTRY_CLIENT.schedule_catalog("nous", upstream_models, "runtime-catalog")
        except Exception:
            pass
        if not upstream_models:
            upstream_models = MODEL_STORE.get_catalog(fresh_only=False)

    models_list = list(upstream_models)

    # N-17 fix: build the new set first, then swap atomically — no transient
    # empty _known_models window for concurrent requests.
    global _known_models
    new_known: Set[str] = set()
    for m in models_list:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            new_known.add(mid)
    for m in CURATED_FREE_MODELS:
        new_known.add(m["id"])
    _known_models = new_known

    # Add the curated discovery manifest only when upstream catalog is unavailable; it is not an inference fallback
    # Codex CLI needs to discover models before making chat requests
    if not upstream_models or len(upstream_models) == 0:
        for m in CURATED_FREE_MODELS:
            if model_allowed(m.get("id", "")):
                models_list.append(m)

    # Inject dynamic aliases (bound to last concrete model if any)
    tgt = get_dynamic_alias_target()
    for alias in sorted(_ALIAS_NAME_SET):
        if free_only_enabled():
            # only show alias if current dynamic target is free (or target unset → skip under FREE_ONLY)
            if not tgt or not (is_free_model(alias) or is_free_model(tgt)):
                continue
        if not any(m.get("id") == alias for m in models_list):
            entry = {"id": alias, "object": "model", "created": 0, "owned_by": "alias", "dynamic_alias": True}
            if tgt:
                entry["rooted_model"] = tgt
            models_list.append(entry)

    # Always inject sonnet/haiku/opus aliases if we have a dynamic target (for Claude Code compatibility)
    if tgt:
        for alias in ("sonnet", "opus", "haiku"):
            if not any(m.get("id") == alias for m in models_list):
                entry = {"id": alias, "object": "model", "created": 0, "owned_by": "alias", "dynamic_alias": True, "rooted_model": tgt}
                models_list.append(entry)

    if free_only_enabled():
        models_list = [m for m in models_list if model_allowed(m.get("id", ""))]
    # Deduplicate by id (upstream + aliases can repeat free models)
    seen = set()
    deduped = []
    for m in models_list:
        mid = m.get("id") if isinstance(m, dict) else None
        if not mid or mid in seen:
            continue
        seen.add(mid)
        deduped.append(m)
    enriched = [get_model_meta(m.get("id", "")) for m in deduped]
    status_map = MODEL_STORE.status_map()
    # Preserve original id on meta and expose account-scoped state without
    # pretending that a catalog entry is globally deployable.
    for i, m in enumerate(deduped):
        if isinstance(enriched[i], dict):
            mid = m.get("id")
            enriched[i]["id"] = mid
            st = status_map.get(mid, {})
            enriched[i]["catalog_listed"] = True
            enriched[i]["availability_state"] = st.get("state", "unknown")
            enriched[i]["availability_scope"] = "account"
            enriched[i]["reason_code"] = st.get("reason_code", "")
            enriched[i]["checked_at"] = st.get("checked_at")
    return {"object": "list", "data": enriched, "models": enriched, "free_only": free_only_enabled(), "dynamic_alias_target": get_dynamic_alias_target() or None, "catalog_cached": bool(MODEL_STORE.get_catalog(fresh_only=True))}

@app.get("/v1/capabilities")
async def capabilities():
    try:
        model_response = await models()
        models_list = model_response.get("data", []) if isinstance(model_response, dict) else []
    except Exception:
        models_list = []
    if not models_list:
        models_list = MODEL_STORE.get_catalog(fresh_only=False) or CURATED_FREE_MODELS
    enriched = []
    for m in models_list:
        mid = m.get("id") if isinstance(m, dict) else m
        meta = get_model_meta(mid)
        enriched.append({
            "id": mid,
            "capabilities": ["chat", "completion"],
            "streaming": True,
            "context_window": meta.get("context_window", 128000),
            "max_tokens": meta.get("max_tokens", 4096),
        })
    tgt = get_dynamic_alias_target()
    return {
        "object": "list",
        "models": enriched,
        "summary": {"total": len(enriched), "by_type": {"chat": len(enriched)}},
        "dynamic_alias_target": tgt or None,
    }

@app.post("/v1/messages/count_tokens")
async def count_tokens(req: Request):
    # N-18 fix: non-discovery endpoint requires auth.
    await _auth_check(req)
    try:
        body = await req.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    est = len(str(body)) // 4
    return {"input_tokens": max(1, est)}

# --- OPENAI CHAT ---
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    import uuid
    import time
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start_time = time.time()
    await _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    # INFO parity fix: request.client can be None (e.g. some test clients /
    # unix sockets) — guard instead of raising AttributeError.
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(429, {"error": {"type": "rate_limit_error", "message": "Too many requests"}})

    # BUG-SEC3 fix (via shared helper, N-11): validate + cap max_tokens
    mt_err = validate_max_tokens_field(body.get('max_tokens'), 'max_tokens')
    if mt_err:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': mt_err}})

    requested = body.get("model")
    # Transparent: only alias-map; do not inject DEFAULT_MODEL
    model = resolve_model(requested) if requested else requested
    if requested:
        body["model"] = model
    if free_only_enabled() and requested and not model_allowed(requested) and not model_allowed(model or ""):
        return JSONResponse(status_code=400, content=free_only_error(requested))
    if free_only_enabled() and model and not model_allowed(model):
        return JSONResponse(status_code=400, content=free_only_error(requested or model))
    for m in body.get('messages', []) or []:
        if isinstance(m, dict) and m.get('role') not in (None, 'system', 'user', 'assistant', 'tool', 'developer', 'function'):
            return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f"Invalid role: {m.get('role')!r} (must be one of: system, user, assistant, tool, developer, function)"}})
        if isinstance(m, dict) and m.get('role') == 'tool' and not m.get('tool_call_id'):
            return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': "tool role requires tool_call_id"}})
    for bad in ["n", "logprobs", "logit_bias", "user", "frequency_penalty", "presence_penalty"]:
        body.pop(bad, None)
    # Drop name:null tools (Codex/Hermes) before upstream
    if isinstance(body.get("tools"), list):
        cleaned = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = fn.get("name") if isinstance(fn, dict) else None
            if not name:
                continue
            cleaned.append(tool)
        if cleaned:
            body["tools"] = cleaned
        else:
            body.pop("tools", None)

    is_stream = body.get("stream", False)
    extra_h = {h: _sanitize_header_value(request.headers.get(h)) for h in ["anthropic-beta", "anthropic-version", "openai-beta", "x-request-id"] if request.headers.get(h)}

    status, result, key_entry = await post_nous_with_retries(body, stream=is_stream, extra_headers=extra_h)
    # F3 round-2 fix: model-state persistence must not delay the response;
    # fire-and-forget with a retained reference (_BG_TASKS pattern).
    _fire_and_forget(record_model_result(body.get("model", ""), key_entry, status, result, "/v1/chat/completions"), "model-result")
    metrics.record(error=(status != 200))

    if status != 200:
        return JSONResponse(status_code=status, content=result)

    if is_stream:
        async def gen():
            async for line in stream_with_heartbeat(result, lambda x: f"data: {json.dumps(x)}\n\n", key_entry=key_entry):
                yield line
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
    if isinstance(result, dict):
        _ensure_chat_content(result)
        if not result.get('usage'):
            result['usage'] = {
                'prompt_tokens': 0,
                'completion_tokens': 0,
                'total_tokens': 0,
            }
    return JSONResponse(result)

# --- OPENAI RESPONSES (FIXED streaming format for Codex/Claude) ---
@app.post("/v1/responses")
async def responses(request: Request):
    await _auth_check(request)
    # N-04 fix: malformed JSON → 400 invalid_request_error, not a generic 500.
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    # N-11 fix: per-IP rate limiting on this endpoint too (SEC sibling gap).
    # INFO parity fix: request.client can be None — guard with "unknown".
    if not check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(429, {"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    # N-11 fix: type-guard + cap max_output_tokens (int("abc") previously → 500).
    mot_err = validate_max_tokens_field(body.get("max_output_tokens"), "max_output_tokens")
    if mot_err:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': mot_err}})
    requested = body.get("model")
    if free_only_enabled() and requested:
        resolved = resolve_model(requested)
        if not model_allowed(requested) and not model_allowed(resolved):
            return JSONResponse(status_code=400, content=free_only_error(requested))
    # BUG-SEC-RESPONSE-STORE fix: extract principal for tenant-isolated store.
    principal = _extract_principal(request)
    chat_body = responses_to_chat(body, principal)
    if free_only_enabled() and chat_body.get("model") and not model_allowed(chat_body.get("model", "")):
        return JSONResponse(status_code=400, content=free_only_error(chat_body.get("model") or requested or ""))
    is_stream = body.get("stream", False)
    extra_h = {h: _sanitize_header_value(request.headers.get(h)) for h in ["anthropic-beta", "anthropic-version", "openai-beta", "x-request-id"] if request.headers.get(h)}

    status, result, key_entry = await post_nous_with_retries(chat_body, stream=is_stream, extra_headers=extra_h, client_surface="openai_responses")
    # F3 round-2 fix: fire-and-forget with retained reference (_BG_TASKS pattern).
    _fire_and_forget(record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/responses"), "model-result")
    # N-20 fix: record metrics on this surface too (was only chat_completions).
    metrics.record(error=(status != 200))
    if status != 200:
        return JSONResponse(status_code=status, content=result)

    if is_stream:
        rid = f"resp-{int(time.time()*1000)}"
        state = ResponsesStreamState(rid, chat_body["model"])
        async def gen():
            # Codex requires output_item.added BEFORE first delta.
            for ev in state.start():
                yield ev
            try:
                async for line in stream_with_heartbeat(result, lambda x: x if isinstance(x, str) else str(x), state=state, key_entry=key_entry):
                    yield line
            finally:
                # BUG-FIX: store conversation without blocking stream
                # finalization. N-07 fix: never await in this finally — on
                # client disconnect (GeneratorExit) an await here suspends the
                # generator during finalization (RuntimeError: async generator
                # ignored GeneratorExit) and can skip persistence entirely.
                # A background task persists the history either way, so the
                # next turn's previous_response_id still finds the assistant
                # tool_calls. N-07 round-2 fix: retain the task reference via
                # _fire_and_forget (_BG_TASKS) so the loop's weak ref cannot
                # let it be GC'd mid-flight, and log its exceptions.
                try:
                    _fire_and_forget(
                        store_conversation(principal, rid, list(chat_body.get("messages", [])) + [state.assistant_message()]),
                        "store-conversation",
                    )
                except Exception as e:
                    logger.warning(f"[responses] store_conversation scheduling failed: {e}")
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    resp = chat_to_responses(chat_body["model"], result)
    # Store the FULL conversation (user input + assistant reply incl. tool_calls)
    # so that a later tool-result turn has the preceding assistant tool_calls —
    # otherwise Nous rejects the orphaned role:tool with 400.
    saved_msgs = list(chat_body.get("messages", []))
    amsg = (result.get("choices") or [{}])[0].get("message", {})
    if amsg:
        saved_msgs.append({
            "role": "assistant",
            "content": amsg.get("content"),
            "tool_calls": amsg.get("tool_calls") or None,
        })
    await store_conversation(principal, resp["id"], saved_msgs)
    return resp

# --- ANTHROPIC MESSAGES ---
@app.post("/v1/messages")
async def messages(request: Request):
    await _auth_check(request)
    # N-04 fix: malformed JSON → 400 invalid_request_error, not a generic 500.
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    # N-11 fix: per-IP rate limiting on this endpoint too (SEC sibling gap).
    # INFO parity fix: request.client can be None — guard with "unknown".
    if not check_rate_limit(request.client.host if request.client else "unknown"):
        raise HTTPException(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Too many requests"}})
    # N-11 fix: apply the shared SEC3 cap+validation (upper bound was missing).
    mt_err = validate_max_tokens_field(body.get('max_tokens'), 'max_tokens', required=True)
    if mt_err:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': mt_err}})
    sys_field = body.get('system')
    if sys_field is not None and not isinstance(sys_field, (str, list)):
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '"system" must be a string or array of content blocks'}})
    for t in body.get('tools', []) or []:
        if not isinstance(t.get('input_schema'), dict):
            return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tool.input_schema must be an object'}})
    requested = body.get("model")
    if free_only_enabled() and requested:
        resolved = resolve_model(requested)
        if not model_allowed(requested) and not model_allowed(resolved):
            return JSONResponse(status_code=400, content=free_only_anthropic_error(requested))
    chat_body = anthropic_to_openai(body)
    # Note: anthropic_to_openai may map thinking→REASONING_MODEL (pre-existing);
    # FREE_ONLY still enforces the *outgoing* model is free when enabled.
    if free_only_enabled() and chat_body.get("model") and not model_allowed(chat_body.get("model", "")):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(chat_body.get("model") or requested or ""))
    is_stream = body.get("stream", False)
    extra_h = {h: _sanitize_header_value(request.headers.get(h)) for h in ["anthropic-beta", "anthropic-version", "openai-beta", "x-request-id"] if request.headers.get(h)}

    status, result, key_entry = await post_nous_with_retries(chat_body, stream=is_stream, extra_headers=extra_h, client_surface="anthropic_messages")
    # F3 round-2 fix: fire-and-forget with retained reference (_BG_TASKS pattern).
    _fire_and_forget(record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/messages"), "model-result")
    # N-20 fix: record metrics on this surface too (was only chat_completions).
    metrics.record(error=(status != 200))
    if status != 200:
        # FIX: Proper Anthropic error format for Claude Code
        err_data = result if isinstance(result, dict) else {"message": str(result)}
        err_msg = err_data.get("error", {}).get("message") if isinstance(err_data.get("error"), dict) else err_data.get("message", str(err_data))
        err_type = err_data.get("error", {}).get("type") if isinstance(err_data.get("error"), dict) else "api_error"
        return JSONResponse(status_code=status, content={"type": "error", "error": {"type": err_type, "message": err_msg}})

    if is_stream:
        state = AnthropicStreamState(chat_body["model"])
        async def gen():
            async for line in stream_with_heartbeat(result, lambda x: f"event: {x.get('type')}\ndata: {json.dumps({**(x.get('data') or {}), **({'type': x.get('type')} if (x.get('data') or {}).get('type') is None else {})})}\n\n", state=state, key_entry=key_entry):
                yield line
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    return openai_to_anthropic(chat_body["model"], result)

# --- METRICS ---
@app.get("/metrics")
async def get_metrics(request: Request):
    # N-18 fix: telemetry endpoints require the bearer token.
    await _auth_check(request)
    return metrics.snapshot()

@app.get("/metrics/prom")
async def prom(request: Request):
    # N-18 fix: telemetry endpoints require the bearer token.
    await _auth_check(request)
    snap = metrics.snapshot()
    lines = [
        f'# HELP wrapper_nous_requests_total Total requests\nwrapper_nous_requests_total {snap["total_requests"]}',
        f'wrapper_nous_tokens_total {snap["total_tokens"]}',
    ]
    return Response("\n".join(lines), media_type="text/plain")

@app.get("/metrics/model-status")
async def model_status(request: Request):
    # N-18 fix: telemetry endpoints require the bearer token.
    await _auth_check(request)
    return {
        "provider": "nous",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": MODEL_STORE.status_map(),
    }


@app.get("/dashboard")
@app.get("/dashboard.html")
async def dashboard(request: Request):
    """Serve the wrapper dashboard HTML.

    N-03 round-2 fix: the HTML shell is secret-free (the BEARER_TOKEN is never
    injected), so it is served WITHOUT auth — a browser cannot attach a bearer
    header on plain navigation, which made the auth-gated page unreachable.
    Auth stays enforced on every API endpoint the page calls; the token is
    entered client-side via the existing sessionStorage prompt.
    """
    from pathlib import Path
    from fastapi.responses import HTMLResponse
    dashboard_path = Path(__file__).parent.parent / "dashboard.html"
    if not dashboard_path.exists():
        return HTMLResponse(content="<html><body><h1>Dashboard not found</h1></body></html>")
    return HTMLResponse(content=dashboard_path.read_text())

@app.get("/healthz")
async def healthz(): return await health()

# catch-all
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    return JSONResponse(status_code=404, content={"error": {"message": f"Unsupported: {path}", "type": "not_found_error"}})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("wrapper_nous:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
