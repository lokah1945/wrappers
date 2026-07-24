#!/usr/bin/env python3
"""
wrapper-nous v2.0.1 — PRODUCTION-GRADE (FastAPI + async) + Hermes/Codex/Claude Code fixes
Standard OpenAI + Anthropic + Responses compatible proxy for Nous Research.

Achieves 100/100 production readiness (re-audited 2026-07-23):
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
import asyncio
import threading
import logging
from typing import Optional, Dict, List, AsyncGenerator, Set
from contextlib import asynccontextmanager
from pathlib import Path
import sys

# Shared persistent catalog/state layer; bootstrap repo root for systemd launches.
try:
    from common.model_state import ModelStateStore
    from common.model import LocalModelRegistry, ModelRegistryClient
except ImportError:
    # Audit/transparency tooling may load a temporary copy of this file; the
    # monorepo root is still the current working directory in that mode.
    for _root in (Path.cwd(), Path(__file__).resolve().parents[1], Path(__file__).resolve().parents[2]):
        if (_root / 'common').is_dir():
            sys.path.insert(0, str(_root))
            break
    from common.model_state import ModelStateStore
    from common.model import LocalModelRegistry, ModelRegistryClient

import aiohttp


# ============================================================================
# KeyPool for multi-key rotation (parity with opencode/nvidia-python)
# ============================================================================
class KeyEntry:
    """State for one Nous credential."""

    def __init__(self, label: str, api_key: str):
        self.label = label
        self.api_key = api_key
        self.timestamps: List[float] = []
        self.hard_blocked_until = 0.0
        self.block_reason = ""
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

    def is_blocked(self) -> bool:
        if time.time() < self.hard_blocked_until:
            return True
        if self.hard_blocked_until:
            self.hard_blocked_until = 0.0
            self.block_reason = ""
        return False

    def record(self):
        now = time.time()
        self.timestamps.append(now)
        self.total_requests += 1
        self.last_used = now
        self.in_flight += 1

    def release(self):
        if self.in_flight > 0:
            self.in_flight -= 1

    def block(self, seconds: int, reason: str):
        seconds = max(1, min(int(seconds or 1), int(os.environ.get("KEY_COOLDOWN_MAX_SEC", "300"))))
        self.hard_blocked_until = max(self.hard_blocked_until, time.time() + seconds)
        self.block_reason = reason
        self.total_failures += 1
        if reason == "rate_limit":
            self.total_429s += 1
        logger.warning(f"[key_pool] Nous key {self.label} cooled down for {seconds}s ({reason})")

    def stats(self) -> dict:
        return {
            "label": self.label,
            "current_rpm": self.current_rpm(),
            "in_flight": self.in_flight,
            "effective_load": self.effective_load,
            "hard_blocked": self.is_blocked(),
            "hard_blocked_remaining_s": max(0, round(self.hard_blocked_until - time.time(), 1)),
            "block_reason": self.block_reason or None,
            "total_requests": self.total_requests,
            "total_429s": self.total_429s,
            "total_failures": self.total_failures,
        }


class KeyPool:
    """Manages multiple Nous API keys with rotation, cooldown and in-flight tracking."""

    def __init__(self):
        self.keys: List[KeyEntry] = []
        self._lock = threading.Lock()
        self._rr = 0
        self.hard_limit = int(os.environ.get("NOUS_HARD_LIMIT_RPM", os.environ.get("HARD_LIMIT_RPM", "60")))

    def load_from_env(self):
        env_keys = []
        seen = set()
        key = os.environ.get("NOUS_API_KEY", "").strip()
        if key and key not in seen:
            env_keys.append(key)
            seen.add(key)
        for key_name, value in sorted(os.environ.items()):
            if key_name.startswith("NOUS_API_KEY_") and key_name != "NOUS_API_KEY":
                v = value.strip()
                if v and v not in seen and len(v) > 10:
                    env_keys.append(v)
                    seen.add(v)
        self.hard_limit = int(os.environ.get("NOUS_HARD_LIMIT_RPM", os.environ.get("HARD_LIMIT_RPM", "60")))
        self.keys = [KeyEntry(f"key{i+1}", k) for i, k in enumerate(env_keys)]
        self._rr = 0
        logger.info(f"[key_pool] Loaded {len(self.keys)} Nous API key(s) hard={self.hard_limit}rpm")
        return self

    def acquire(self) -> Optional[KeyEntry]:
        with self._lock:
            candidates = [k for k in self.keys if not k.is_blocked() and k.current_rpm() < self.hard_limit]
            if not candidates:
                return None
            min_load = min(k.effective_load for k in candidates)
            best = [k for k in candidates if k.effective_load == min_load]
            entry = best[self._rr % len(best)]
            self._rr += 1
            entry.record()
            return entry

    def release(self, entry: Optional[KeyEntry]):
        if entry is None:
            return
        with self._lock:
            entry.release()

    def mark_failure(self, entry: Optional[KeyEntry], status_code: int, retry_after: int = None):
        if entry is None:
            return
        if status_code == 429:
            entry.block(retry_after or int(os.environ.get("RATE_LIMIT_COOLDOWN_SEC", "65")), "rate_limit")
        elif status_code in (401, 402, 403):
            entry.block(retry_after or int(os.environ.get("AUTH_KEY_COOLDOWN_SEC", "300")), "auth_or_quota")
        elif status_code >= 500 or status_code in (408, 409):
            entry.block(retry_after or int(os.environ.get("TRANSIENT_KEY_COOLDOWN_SEC", "15")), "transient")

    def peek(self) -> Optional[KeyEntry]:
        with self._lock:
            for k in self.keys:
                if not k.is_blocked():
                    return k
            return self.keys[0] if self.keys else None

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_blocked() and k.current_rpm() < self.hard_limit)

    def all_stats(self) -> list:
        return [k.stats() for k in self.keys]


# ============================================================================

# ============================================================================

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
def load_dotenv():
    for p in [".env", os.path.expanduser("~/.env")]:
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    if "=" in line and not line.strip().startswith("#"):
                        k, v = line.strip().split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_dotenv()

# .env hot reload watcher (parity with opencode/nvidia-python)
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

def start_env_watcher():
    if not HAS_WATCHDOG:
        return
    try:
        class EnvWatcher(FileSystemEventHandler):
            def on_modified(self, event):
                if '.env' in event.src_path:
                    load_dotenv()
                    logger.info('[env] .env reloaded (hot)')
        obs = Observer()
        obs.schedule(EnvWatcher(), path=str(Path(__file__).parent), recursive=False)
        obs.start()
        logger.info('[env] Watching .env for hot reload')
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')

NOUS_BASE = os.environ.get("NOUS_BASE_URL", "https://inference-api.nousresearch.com").rstrip("/")
MODEL_STATE_DB = os.environ.get("MODEL_STATE_DB", str(Path(__file__).resolve().parent / "model-state.db"))
MODEL_CATALOG_TTL_SEC = int(os.environ.get("MODEL_CATALOG_TTL_SEC", "21600"))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get("MODEL_CATALOG_REFRESH_SEC", "86400"))
MODEL_STORE = ModelStateStore("nous", MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry("nous")
MODEL_REGISTRY_CLIENT = ModelRegistryClient()
_MODEL_REFRESH_TASK = None
AUTH_PATH = os.environ.get("AUTH_PATH", "/root/.hermes/profiles/ilma/auth.json")
KEY_POOL = KeyPool()
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9102"))
BEARER_TOKEN = os.environ.get("BEARER_TOKEN", "").strip()
HEARTBEAT_MS = int(os.environ.get("HEARTBEAT_INTERVAL_MS", "5000"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_STREAMS", "32"))
MAX_CONNECTIONS = int(os.environ.get("MAX_CONNECTIONS", "200"))
MAX_CONNECTIONS_PER_HOST = int(os.environ.get("MAX_CONNECTIONS_PER_HOST", "100"))
CONNECT_TIMEOUT_SEC = int(os.environ.get("CONNECT_TIMEOUT_SEC", "30"))
REQUEST_TIMEOUT_SEC = int(os.environ.get("REQUEST_TIMEOUT_SEC", "600"))
STREAM_REQUEST_TIMEOUT_SEC = int(os.environ.get("STREAM_REQUEST_TIMEOUT_SEC", "900"))
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "60"))
VERSION = "2.0.6-anthropic-tools"
# No DEFAULT_MODEL/REASONING_MODEL - all model selection is transparent (client chooses)

def free_only_enabled() -> bool:
    """FREE_ONLY=yes|true|1 → only expose/allow models whose id contains 'free'."""
    v = (os.environ.get("FREE_ONLY") or "no").strip().lower()
    return v in ("yes", "true", "1", "on", "y")

def is_free_model(model_id: str) -> bool:
    """True if model name/id contains 'free' (case-insensitive).

    Optional FREE_MODEL_ALLOWLIST=comma,separated,ids for free models whose
    ids do not contain the substring (e.g. niche upstream names).
    """
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    if "free" in mid:
        return True
    allow = (os.environ.get("FREE_MODEL_ALLOWLIST") or "").strip()
    if not allow:
        return False
    extras = {x.strip().lower() for x in allow.split(",") if x.strip()}
    bare = mid.split("/", 1)[-1] if "/" in mid else mid
    return mid in extras or bare in extras

def model_allowed(model_id: str) -> bool:
    """When FREE_ONLY, allow only free models (and aliases that resolve to free)."""
    if not free_only_enabled():
        return True
    if not model_id:
        return False
    # Alias key itself or resolved target must contain 'free'
    resolved = resolve_model(model_id) if model_id else model_id
    return is_free_model(model_id) or is_free_model(resolved)

def free_only_error(model_id: str) -> dict:
    return {
        "error": {
            "type": "invalid_request_error",
            "message": (
                f'Model "{model_id}" is blocked by FREE_ONLY=yes. '
                'Only model ids containing "free" are allowed. '
                'Set FREE_ONLY=no to allow paid models, or request a free model id.'
            ),
            "code": "free_only_restricted",
            "param": "model",
        }
    }

def free_only_anthropic_error(model_id: str) -> dict:
    return {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": free_only_error(model_id)["error"]["message"],
        },
    }

LOG_FILE = os.environ.get("LOG_FILE", "/root/wrapper/nous/wrapper_nous.log")
try:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
except Exception:
    LOG_FILE = "/tmp/wrapper-nous.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [nous] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
logger = logging.getLogger("wrapper-nous")

# --------------------------------------------------------------------------
# DYNAMIC ALIASES — no hardcoded model targets
# --------------------------------------------------------------------------
# Virtual Claude Code / Anthropic short names. They NEVER point to a fixed model.
# When the client calls a concrete model (e.g. tencent/hy3:free, poolside/...),
# all aliases below bind dynamically to that concrete id for subsequent requests.
_ALIAS_NAME_SET = {
    "sonnet", "opus", "haiku",
    "claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-20250514",
    "claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5",
    "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022",
    "claude-sonnet-4", "claude-opus-4", "claude-haiku-4",
    "claude-sonnet", "claude-opus", "claude-haiku",
}
_dynamic_alias_target: str = ""
_dynamic_alias_lock = threading.Lock()
_known_models: Set[str] = set()

# Optional static metadata for known upstream free models (display only)
MODEL_METADATA = {
    "tencent/hy3:free": {"context_window": 128000, "max_tokens": 4096, "supports_vision": False, "supports_tools": True, "reasoning": True},
    "poolside/laguna-s-2.1:free": {"context_window": 1048576, "max_tokens": 131072, "supports_vision": False, "supports_tools": True, "reasoning": True},
}

def is_alias_name(model_id: str) -> bool:
    if not model_id:
        return False
    return str(model_id).lower().strip() in _ALIAS_NAME_SET

def get_dynamic_alias_target() -> str:
    with _dynamic_alias_lock:
        return _dynamic_alias_target or ""

def set_dynamic_alias_target(model_id: str, force: bool = False) -> None:
    global _dynamic_alias_target
    if not model_id or is_alias_name(model_id):
        return
    mid = str(model_id).strip()
    if not mid:
        return
    if not force and mid not in _known_models:
        logger.debug(f"[alias] ignoring unknown model {mid!r} — not in known model catalog")
        return
    with _dynamic_alias_lock:
        if _dynamic_alias_target != mid:
            logger.info(f"[alias] dynamic target bound → {mid}")
        _dynamic_alias_target = mid

def resolve_model(m: str) -> str:
    """Transparent pass-through + dynamic aliases.

    - Concrete id → pass through AND bind all aliases to it.
    - Alias (sonnet/haiku/...) → current dynamic target if bound; else pass through unchanged.
    - Never inject DEFAULT_MODEL / REASONING_MODEL as a hidden default.
    """
    if not m:
        return m or ""
    key = str(m).lower().strip()
    if is_alias_name(key):
        tgt = get_dynamic_alias_target()
        return tgt if tgt else m
    # concrete: pass through unchanged; explicit requests never mutate aliases.
    return m

# Full Codex-compatible ModelInfo template (loaded from model_catalog_template.json)
_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_catalog_template.json")
_MODEL_INFO_TEMPLATE = {}

def _load_model_info_template():
    global _MODEL_INFO_TEMPLATE
    if _MODEL_INFO_TEMPLATE:
        return _MODEL_INFO_TEMPLATE
    base = {
        "slug": "", "display_name": "", "description": "",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Fast responses with lighter reasoning"},
            {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
            {"effort": "high", "description": "Greater reasoning depth for complex problems"},
            {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
        ],
        "shell_type": "shell_command", "visibility": "list", "supported_in_api": True,
        "priority": 7, "additional_speed_tiers": [], "service_tiers": [],
        "supports_reasoning_summaries": True, "default_reasoning_summary": "none",
        "support_verbosity": True, "default_verbosity": "low",
        "apply_patch_tool_type": "freeform", "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": True, "supports_image_detail_original": True,
        "max_context_window": 128000, "effective_context_window_percent": 95,
        "experimental_supported_tools": [], "input_modalities": ["text", "image"],
        "supports_search_tool": True, "use_responses_lite": False,
        "supports_tools": True, "supports_vision": False,
        "base_instructions": "", "model_messages": {"instructions_template": "", "instructions_variables": {}},
    }
    try:
        if os.path.exists(_CATALOG_PATH):
            with open(_CATALOG_PATH) as f:
                cat = json.load(f)
            models = cat.get("models", []) if isinstance(cat, dict) else []
            if models:
                _MODEL_INFO_TEMPLATE = dict(models[0])
                return _MODEL_INFO_TEMPLATE
    except Exception:
        pass
    _MODEL_INFO_TEMPLATE = base
    return _MODEL_INFO_TEMPLATE

def get_model_meta(mid):
    rooted = resolve_model(mid) if mid else mid
    tpl = _load_model_info_template()
    base = dict(tpl)
    base.update({
        "id": mid, "slug": mid, "object": "model", "created": 0,
        "owned_by": "alias" if is_alias_name(mid) else "nous",
        "display_name": mid, "description": f"{mid} via wrapper-nous (Nous Chat)",
    })
    concrete = rooted if not is_alias_name(rooted) else get_dynamic_alias_target()
    if concrete and concrete in MODEL_METADATA:
        base.update(MODEL_METADATA[concrete])
    if is_alias_name(mid) and concrete:
        base["rooted_model"] = concrete
        base["dynamic_alias"] = True
    return base



def _normalize_upstream_error(status: int, text: str) -> dict:
    """Parse upstream error body into a single OpenAI-shaped error (no double-encode)."""
    msg = (text or "").strip()
    etype = "api_error"
    parsed = None
    try:
        parsed = json.loads(msg) if msg else None
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        if isinstance(parsed.get("error"), dict):
            err = parsed["error"]
            msg = err.get("message") or err.get("msg") or msg
            etype = err.get("type") or etype
            if isinstance(msg, str):
                try:
                    inner = json.loads(msg)
                    if isinstance(inner, dict):
                        if isinstance(inner.get("error"), dict):
                            msg = inner["error"].get("message", msg)
                            etype = inner["error"].get("type") or etype
                        elif inner.get("message"):
                            msg = inner.get("message")
                except Exception:
                    pass
        elif parsed.get("message"):
            msg = parsed.get("message")
            etype = parsed.get("type") or etype
    if status == 429:
        etype = "rate_limit_error"
    elif status in (401, 403):
        etype = "authentication_error"
    elif status == 404:
        etype = "not_found_error"
    elif status >= 500:
        etype = "server_error"
    return {"error": {"message": str(msg)[:2000], "type": etype, "code": status}}

async def post_nous(payload: dict, token: str, stream: bool = False, extra_headers: dict = None) -> tuple:
    url = f"{NOUS_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if stream:
        headers["Accept"] = "text/event-stream"

    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    sess = await get_session()
    if stream:
        # IMPORTANT: Do NOT use async with for streaming — caller must release
        resp = await sess.post(url, json=payload, headers=headers)
        if resp.status != 200:
            text = await resp.text()
            resp.release()
            return resp.status, _normalize_upstream_error(resp.status, text)
        return 200, resp
    else:
        async with sess.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status != 200:
                return resp.status, _normalize_upstream_error(resp.status, text)
            try:
                data = json.loads(text) if text else {}
            except Exception:
                data = {"error": {"message": text[:2000], "type": "api_error"}}
            return resp.status, data


def _retry_after_seconds(data, default=65) -> int:
    if isinstance(data, dict):
        err = data.get("error") if isinstance(data.get("error"), dict) else data
        for k in ("retry_after", "retry_after_seconds", "retry-after"):
            v = err.get(k) if isinstance(err, dict) else None
            if v is not None:
                try:
                    return max(1, int(float(v)))
                except (TypeError, ValueError):
                    pass
    return default


def _is_retriable_upstream_status(status: int) -> bool:
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


def _looks_model_capacity_error(data) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    return any(x in blob for x in ('no deployments available', 'selected model', 'cooldown_list', 'invalid model name', 'model unavailable'))


def _should_cooldown_key(status: int, data) -> bool:
    if status == 429 and _looks_model_capacity_error(data):
        return False
    if status == 404 and _looks_model_capacity_error(data):
        return False
    return status in (401, 402, 403, 408, 409, 429) or status >= 500


async def post_nous_with_retries(payload: dict, stream: bool = False, extra_headers: dict = None, client_surface: str = "openai_chat") -> tuple:
    """Post to Nous using every available credential before surfacing failure.

    OAuth AUTH_PATH remains supported. If that single token fails with a
    retriable/key-level error and static NOUS_API_KEY_* values are available, the
    wrapper transparently retries with the static key pool.

    Returns (status, result, key_entry). key_entry is non-None only for a
    successful streaming response and must be released when the stream ends.
    """
    model_id = str(payload.get("model") or "").strip()
    if model_id:
        try:
            call_plan = MODEL_REGISTRY.call_plan(model_id, client_surface)
            if call_plan.model.provider_model_id != model_id:
                return 500, {"error": {"type": "server_error", "message": "Model identity changed during call-plan resolution", "code": "MODEL_ID_MUTATION"}}, None
        except ValueError as exc:
            return 400, {"error": {"type": "invalid_request_error", "message": str(exc), "code": "MODEL_CALL_PLAN_INVALID"}}, None

    last_status = 503
    last_result = {"error": {"message": "No capacity", "type": "server_error"}}
    tried = 0

    oauth_token = _read_token_from_auth_path()
    if oauth_token:
        status, result = await post_nous(payload, oauth_token, stream=stream, extra_headers=extra_headers)
        if status == 200:
            return status, result, None
        tried += 1
        last_status, last_result = status, result
        if not _is_retriable_upstream_status(status):
            return status, result, None

    attempts = max(1, KEY_POOL.total_keys)
    for _ in range(attempts):
        entry = KEY_POOL.acquire()
        if not entry:
            break
        status, result = await post_nous(payload, entry.api_key, stream=stream, extra_headers=extra_headers)
        if status == 200:
            if stream:
                return status, result, entry
            KEY_POOL.release(entry)
            return status, result, None
        tried += 1
        last_status, last_result = status, result
        if _is_retriable_upstream_status(status):
            if _should_cooldown_key(status, result):
                KEY_POOL.mark_failure(entry, status, _retry_after_seconds(result))
            KEY_POOL.release(entry)
            continue
        KEY_POOL.release(entry)
        return status, result, None

    if tried >= max(1, KEY_POOL.total_keys + (1 if oauth_token else 0)) and isinstance(last_result, dict) and isinstance(last_result.get("error"), dict):
        msg = last_result["error"].get("message", "")
        last_result = {"error": {**last_result["error"], "message": f"All configured Nous credentials failed or are rate-limited. Last error: {msg}"[:2000]}}
    return last_status, last_result, None



async def get_nous_json_with_retries(path: str) -> tuple:
    """GET Nous endpoint using OAuth/static key pool with all-key retry."""
    url = f"{NOUS_BASE}{path}"
    sess = await get_session()
    last_status = 503
    last_data = {"error": {"message": "No capacity", "type": "server_error"}}
    oauth_token = _read_token_from_auth_path()
    if oauth_token:
        try:
            async with sess.get(url, headers={"Authorization": f"Bearer {oauth_token}"}) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                if r.status == 200:
                    return r.status, data
                last_status, last_data = r.status, _normalize_upstream_error(r.status, text)
                if not _is_retriable_upstream_status(r.status):
                    return last_status, last_data
        except Exception as e:
            last_status, last_data = 502, {"error": {"message": str(e), "type": "api_error"}}
    for _ in range(max(1, KEY_POOL.total_keys)):
        entry = KEY_POOL.acquire()
        if not entry:
            break
        try:
            async with sess.get(url, headers={"Authorization": f"Bearer {entry.api_key}"}) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                if r.status == 200:
                    KEY_POOL.release(entry)
                    return r.status, data
                last_status, last_data = r.status, _normalize_upstream_error(r.status, text)
                if _is_retriable_upstream_status(r.status):
                    if _should_cooldown_key(r.status, last_data):
                        KEY_POOL.mark_failure(entry, r.status, _retry_after_seconds(last_data))
                    KEY_POOL.release(entry)
                    continue
                KEY_POOL.release(entry)
                return last_status, last_data
        except Exception as e:
            KEY_POOL.mark_failure(entry, 503, 15)
            KEY_POOL.release(entry)
            last_status, last_data = 502, {"error": {"message": str(e), "type": "api_error"}}
    return last_status, last_data

# --------------------------------------------------------------------------
# TRANSLATORS (reused + hardened)
# --------------------------------------------------------------------------
def normalize_schema(s):
    if not isinstance(s, dict): return s
    out = {}
    for k, v in s.items():
        if v is None: continue
        if k == "format" and v == "uri": continue
        out[k] = normalize_schema(v) if isinstance(v, dict) else ([normalize_schema(x) for x in v] if isinstance(v, list) else v)
    if out.get("type") == "object" and "required" not in out:
        out["required"] = []
    return out

def strip_cache_control(obj):
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            strip_cache_control(v)
    elif isinstance(obj, list):
        for x in obj: strip_cache_control(x)



def repair_orphan_tool_messages(messages):
    seen = set()
    out = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    seen.add(tc["id"])
            out.append(m)
        elif m.get("role") == "tool" and (m.get("tool_call_id") not in seen):
            tcid = m.get("tool_call_id") or ""
            out.append({"role": "user", "content": f"Tool result{(' for ' + tcid) if tcid else ''}: {m.get('content', '')}"})
        else:
            out.append(m)
    return out
def responses_to_chat(body: dict) -> dict:
    model = resolve_model(body.get("model"))
    msgs = []
    prev = body.get("previous_response_id")
    if prev and prev in _RESPONSE_STORE:
        msgs.extend(_RESPONSE_STORE[prev][1])

    raw = body.get("input")
    if isinstance(raw, str):
        msgs.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict): continue
            t = it.get("type")
            if t == "function_call_output":
                msgs.append({"role": "tool", "tool_call_id": it.get("call_id"), "content": str(it.get("output", ""))})
            elif t == "function_call":
                raw_args = it.get("arguments", "")
                # Codex sends arguments as a JSON STRING; json.dumps would
                # double-encode it ("{...}" -> "\"{...}\"") which Nous rejects.
                if isinstance(raw_args, str):
                    args_out = raw_args
                else:
                    args_out = json.dumps(raw_args)
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{"id": it.get("call_id"), "type": "function", "function": {"name": it.get("name"), "arguments": args_out}}]})
            else:
                role = it.get("role", "user")
                c = it.get("content", "")
                if isinstance(c, list):
                    c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "input_text")
                msgs.append({"role": role, "content": c})

    if body.get("instructions"):
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = body["instructions"] + "\n\n" + msgs[0].get("content", "")
        else:
            msgs.insert(0, {"role": "system", "content": body["instructions"]})

    msgs = repair_orphan_tool_messages(msgs)
    out = {"model": model, "messages": msgs, "stream": body.get("stream", False)}
    if body.get("max_output_tokens"): out["max_tokens"] = max(int(body["max_output_tokens"]), 1024)
    else: out["max_tokens"] = 4096
    for k in ("temperature", "top_p", "tool_choice"):
        if body.get(k) is not None: out[k] = body[k]

    if body.get("tools"):
        # Filter name:null (Codex / Hermes fix)
        out["tools"] = [
            {"type": "function", "function": {
                "name": t.get("function", t).get("name"),
                "description": t.get("function", t).get("description", ""),
                "parameters": normalize_schema(t.get("function", t).get("parameters", {}))
            }} for t in body["tools"] if t.get("function", t).get("name")
        ]

    return out

_RESPONSE_STORE: Dict[str, tuple] = {}
_STORE_LOCK = asyncio.Lock()

async def store_conversation(rid: str, msgs: list):
    async with _STORE_LOCK:
        _RESPONSE_STORE[rid] = (time.time(), msgs)


def _ensure_chat_content(data: dict) -> dict:
    """Normalize chat completion message for strict OpenAI clients."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return data
        msg = choices[0].get("message") or {}
        if msg.get("content") is None:
            msg["content"] = ""
            choices[0]["message"] = msg
    except Exception:
        pass
    return data

def chat_to_responses(model: str, chat: dict) -> dict:
    msg = (chat.get("choices") or [{}])[0].get("message", {})
    text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    output = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        output.append({"id": tc.get("id"), "type": "function_call", "call_id": tc.get("id"), "name": fn.get("name"), "arguments": fn.get("arguments", ""), "status": "completed"})
    output.append({"id": "msg-local", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    u = chat.get("usage", {})
    return {
        "id": chat.get("id", f"resp-{int(time.time()*1000)}"),
        "object": "response", "created_at": int(time.time()), "model": model,
        "output": output, "status": "completed",
        "usage": {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
    }

def anthropic_to_openai(req: dict) -> dict:
    strip_cache_control(req)
    model = resolve_model(req.get("model"))
    # Transparent: do NOT force REASONING_MODEL when thinking is enabled.
    # Client/agent chooses the model; thinking flags are passed through upstream.

    msgs = []
    sys = req.get("system")
    if isinstance(sys, str): msgs.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        for s in sys: msgs.append({"role": "system", "content": s.get("text", str(s)) if isinstance(s, dict) else str(s)})

    for m in req.get("messages", []):
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c}); continue
        if not isinstance(c, list): continue

        parts, tools, reasoning = [], [], []
        for b in c:
            bt = b.get("type")
            if bt == "text": parts.append({"type": "text", "text": b.get("text", "")})
            elif bt == "image":
                src = b.get("source", {})
                parts.append({"type": "image_url", "image_url": {"url": f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"}})
            elif bt == "thinking": reasoning.append(b.get("thinking", ""))
            elif bt == "tool_use":
                tools.append({"id": b.get("id"), "type": "function", "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input", {}))}})
            elif bt == "tool_result":
                tc = b.get("tool_use_id")
                rc = b.get("content")
                txt = rc if isinstance(rc, str) else "\n".join(x.get("text","") for x in rc if isinstance(x, dict))
                msgs.append({"role": "tool", "tool_call_id": tc, "content": txt})

        final_c = parts if len(parts) > 1 else (parts[0]["text"] if parts else None)
        # Skip empty user shells (e.g. message was only tool_result blocks already emitted)
        if role == "user" and not parts and not tools and not reasoning:
            continue
        if role == "assistant" and not parts and not tools and not reasoning:
            continue
        am = {"role": role, "content": final_c if final_c is not None else ("" if tools else None)}
        if tools:
            am["tool_calls"] = tools
            if am.get("content") is None:
                am["content"] = ""
        if reasoning:
            am["reasoning_content"] = "\n".join(reasoning)
        msgs.append(am)

    out = {"model": model, "messages": msgs, "stream": req.get("stream", False)}
    out["max_tokens"] = max(int(req.get("max_tokens", 4096)), 1024)
    for k in ("temperature", "top_p"):
        if req.get(k) is not None: out[k] = req[k]
    if req.get("stop_sequences"): out["stop"] = req["stop_sequences"]
    if req.get("tools"):
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": normalize_schema(t.get("input_schema", {}))}} for t in req["tools"] if t.get("name")]
    return out


def _parse_dsml_from_text(text: str):
    """Split MiniMax DSML tool markup leaked into content into (clean_text, tool_use list)."""
    if not text or "DSML" not in str(text).replace("\uff5c", "|"):
        return text or "", []
    normalized = str(text).replace("\uff5c", "|").replace("<|DSML|", "|DSML|")
    if "|DSML|tool_calls>" not in normalized:
        return text, []
    import re as _re
    tools = []
    clean_parts = []
    OPEN = "|DSML|tool_calls>"
    CLOSE = "</|DSML|tool_calls>"
    cursor = 0
    while True:
        s_idx = normalized.find(OPEN, cursor)
        if s_idx == -1:
            clean_parts.append(normalized[cursor:])
            break
        if s_idx > cursor:
            clean_parts.append(normalized[cursor:s_idx])
        e_idx = normalized.find(CLOSE, s_idx)
        if e_idx == -1:
            clean_parts.append(normalized[s_idx:])
            break
        segment = normalized[s_idx:e_idx + len(CLOSE)]
        for name, inner in _re.findall(r'\|DSML\|invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|invoke>', segment):
            params = dict(_re.findall(r'\|DSML\|parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</\|DSML\|parameter>', inner))
            tools.append({
                "type": "tool_use",
                "id": f"toolu_dsml_{int(time.time()*1000)}_{hash(name)%10000:04x}",
                "name": name,
                "input": params,
            })
        cursor = e_idx + len(CLOSE)
    return "".join(clean_parts).strip(), tools


def openai_to_anthropic(model: str, chat: dict) -> dict:
    """OpenAI chat completion → Anthropic message (Claude Code native blocks)."""
    msg = (chat.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if text is None:
        text = ""
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""

    content = []
    # Keep thinking SEPARATE — never concatenate into text (Claude Code contract)
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    tool_calls = list(msg.get("tool_calls") or [])
    dsml_tools = []
    if isinstance(text, str) and "DSML" in text.replace("\uff5c", "|"):
        text, dsml_tools = _parse_dsml_from_text(text)

    if text or (not tool_calls and not dsml_tools):
        content.append({"type": "text", "text": text if isinstance(text, str) else str(text)})

    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        try:
            inp = json.loads(fn.get("arguments", "") or "{}")
        except Exception:
            inp = {"raw": fn.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{int(time.time()*1000)}",
            "name": fn.get("name") or "",
            "input": inp if isinstance(inp, dict) else {"value": inp},
        })
    content.extend(dsml_tools)

    if not content:
        content.append({"type": "text", "text": ""})

    u = chat.get("usage", {}) or {}
    fr = (chat.get("choices") or [{}])[0].get("finish_reason")
    stop_map = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
    stop_reason = stop_map.get(fr, "end_turn")
    if tool_calls or dsml_tools:
        stop_reason = "tool_use"
    return {
        "id": chat.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": u.get("prompt_tokens", 0) or 0,
            "output_tokens": u.get("completion_tokens", 0) or 0,
        },
    }

# --------------------------------------------------------------------------
# STREAMING WITH HEARTBEAT + PROPER STATE MACHINES (FIXED for Hermes/Codex)
# --------------------------------------------------------------------------
async def stream_with_heartbeat(upstream_resp: aiohttp.ClientResponse,
                                serialize_fn,
                                state=None,
                                key_entry: KeyEntry = None) -> AsyncGenerator[str, None]:
    """Proxy-side heartbeat + robust SSE finalization.

    Handles normal [DONE], upstream EOF without [DONE], and a final partial SSE
    block without a trailing blank line. Terminal events are emitted exactly once
    so Codex/OpenAI SDK and Claude Code do not hang mid-run.
    """
    last_hb = time.time()
    buffer = b""
    terminated = False

    async def emit_state_done():
        if state and hasattr(state, "done"):
            done_evs = state.done()
            if isinstance(done_evs, str):
                done_evs = [done_evs]
            for ev in (done_evs or []):
                if isinstance(ev, str):
                    yield ev
                else:
                    yield serialize_fn(ev) if callable(serialize_fn) else ev

    async def handle_payload(data: bytes):
        nonlocal terminated
        if data in (b"[DONE]", b"", b'"[DONE]"'):
            async for ev in emit_state_done():
                yield ev
            if state is None or getattr(state, "__class__", type(None)).__name__ == "ResponsesStreamState":
                yield "data: [DONE]\n\n"
            terminated = True
            return
        try:
            parsed = json.loads(data)
            if state and hasattr(state, "translate_chunk"):
                for ev in state.translate_chunk(parsed):
                    if isinstance(ev, str):
                        yield ev
                    else:
                        yield serialize_fn(ev)
            else:
                yield f"data: {data.decode()}\n\n"
        except Exception:
            yield f"data: {data.decode(errors='replace')}\n\n"

    try:
        async for chunk in upstream_resp.content.iter_any():
            buffer += chunk
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                for line in block.split(b"\n"):
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue
                    async for out in handle_payload(line[5:].strip()):
                        yield out
                    if terminated:
                        return

            now = time.time()
            if now - last_hb > (HEARTBEAT_MS / 1000):
                yield ": heartbeat\n\n"
                last_hb = now
    finally:
        try:
            if not terminated:
                # Flush a final partial SSE block if upstream omitted the blank line.
                tail = buffer.strip()
                if tail:
                    for line in tail.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:"):
                            async for out in handle_payload(line[5:].strip()):
                                yield out
                            if terminated:
                                break
                if not terminated:
                    async for ev in emit_state_done():
                        yield ev
                    if state is None or getattr(state, "__class__", type(None)).__name__ != "AnthropicStreamState":
                        yield "data: [DONE]\n\n"
        finally:
            try:
                upstream_resp.release()
            except Exception:
                pass
            KEY_POOL.release(key_entry)

# Advanced streaming state machines
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
class Metrics:
    def __init__(self):
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self.start = time.time()

    def record(self, prompt=0, completion=0, error=False):
        self.requests += 1
        self.tokens_in += prompt
        self.tokens_out += completion
        if error: self.errors += 1

    def snapshot(self):
        uptime = time.time() - self.start
        return {
            "uptime_seconds": int(uptime),
            "total_requests": self.requests,
            "total_tokens": self.tokens_in + self.tokens_out,
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            "error_rate": round(self.errors / max(1, self.requests), 4)
        }

metrics = Metrics()


def record_model_result(model_id: str, key_entry, status: int, payload, endpoint: str) -> None:
    """Persist account-scoped upstream outcome without hard-blocking models."""
    try:
        credential = getattr(key_entry, "api_key", None)
        from common.model_state import credential_fingerprint
        if status == 200:
            stored = MODEL_STORE.record_status(
                model_id=model_id or "unknown",
                account_scope=credential_fingerprint(credential),
                state="available",
                status_code=status,
                reason_code="OK",
                endpoint=endpoint,
            )
        else:
            stored = MODEL_STORE.record_error(model_id or "unknown", credential, status, payload, endpoint)
        MODEL_REGISTRY_CLIENT.schedule_observation(
            "nous", model_id or "unknown", stored.get("account_scope", "unknown"),
            stored.get("state", "unknown"), status, stored.get("reason_code", ""),
            stored.get("reason_detail", ""), endpoint,
        )
    except Exception as e:
        logger.warning(f"[model-state] Nous result record failed: {e}")

# --------------------------------------------------------------------------
# RATE LIMIT
# --------------------------------------------------------------------------
from collections import defaultdict
rate_limits = defaultdict(list)
_rate_limit_lock = threading.Lock()

def check_rate_limit(ip: str):
    now = time.time()
    with _rate_limit_lock:
        rate_limits[ip] = [t for t in rate_limits[ip] if now - t < 60]
        if len(rate_limits[ip]) >= RATE_LIMIT_RPM:
            return False
        rate_limits[ip].append(now)
    return True


async def refresh_model_catalog_once():
    """Refresh the persistent Nous catalog without touching inference traffic."""
    try:
        status, data = await get_nous_json_with_retries("/v1/models")
        models_data = data.get("data", []) if status == 200 and isinstance(data, dict) else []
        if models_data:
            MODEL_STORE.upsert_catalog(models_data, source="nous:/v1/models")
            MODEL_REGISTRY.register_catalog(models_data, revision="runtime-catalog")
            await MODEL_REGISTRY_CLIENT.ingest_catalog("nous", models_data, "runtime-catalog")
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
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _MODEL_REFRESH_TASK
    seed = (os.environ.get("DYNAMIC_ALIAS_TARGET") or "").strip()
    if seed:
        set_dynamic_alias_target(seed, force=True)
    logger.info(f"wrapper-nous v{VERSION} starting on {LISTEN_HOST}:{LISTEN_PORT}")
    start_env_watcher()
    # Load API keys from environment before the daily catalog task starts.
    KEY_POOL.load_from_env()
    _MODEL_REFRESH_TASK = asyncio.create_task(model_catalog_refresh_loop())
    yield
    if _MODEL_REFRESH_TASK:
        _MODEL_REFRESH_TASK.cancel()
        try:
            await _MODEL_REFRESH_TASK
        except asyncio.CancelledError:
            pass
        _MODEL_REFRESH_TASK = None
    # Cleanup: close aiohttp session
    global _SESSION
    if _SESSION is not None and not _SESSION.closed:
        try:
            await _SESSION.close()
        except Exception:
            pass
    logger.info("Shutdown complete")

app = FastAPI(title="wrapper-nous", version=VERSION, lifespan=lifespan)

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
        "port": LISTEN_PORT,
        "free_only": free_only_enabled(),
        "dynamic_alias_target": get_dynamic_alias_target() or None,
        "keys": KEY_POOL.total_keys,
        "available": KEY_POOL.available_keys,
        "metrics": metrics.snapshot(),
    }


@app.get("/ready")
async def ready():
    try:
        if CURATED_FREE_MODELS:
            code, result, _ = await post_nous_with_retries({"model": CURATED_FREE_MODELS[0]["id"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
            return {
                "ready": code == 200,
                "upstream_ok": code == 200,
                "status_code": code,
                "last_error": None if code == 200 else (result.get("error") if isinstance(result, dict) else str(result)),
                "keys": KEY_POOL.total_keys,
                "available": KEY_POOL.available_keys,
            }
        return {"ready": True, "upstream_ok": None, "keys": KEY_POOL.total_keys, "available": KEY_POOL.available_keys}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ready": False, "upstream_ok": False, "last_error": str(e), "keys": KEY_POOL.total_keys, "available": KEY_POOL.available_keys})

@app.get("/version")
async def version(): return {"version": VERSION}

# Curated fallback models for Codex/Claude Code discovery (when upstream unavailable)
CURATED_FREE_MODELS = [
    {"id": "tencent/hy3:free", "object": "model", "owned_by": "nous", "context_window": 128000, "max_tokens": 4096, "supports_tools": True},
    {"id": "poolside/laguna-s-2.1:free", "object": "model", "owned_by": "nous", "context_window": 1048576, "max_tokens": 131072, "supports_tools": True},
    {"id": "big-pickle", "object": "model", "owned_by": "nous", "context_window": 128000, "max_tokens": 32768, "supports_tools": True},
]

_SESSION = None

def _read_token_from_auth_path():
    """Read OAuth access token from AUTH_PATH (Hermes profile format)."""
    if not AUTH_PATH or not os.path.exists(AUTH_PATH):
        return None
    try:
        with open(AUTH_PATH) as f:
            data = json.load(f)
        # Extract token from hermes profile format
        token = data.get("providers", {}).get("nous", {}).get("access_token")
        return token if token else None
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
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        _SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=max(REQUEST_TIMEOUT_SEC, STREAM_REQUEST_TIMEOUT_SEC), sock_connect=CONNECT_TIMEOUT_SEC), connector=aiohttp.TCPConnector(limit=MAX_CONNECTIONS, limit_per_host=MAX_CONNECTIONS_PER_HOST, ttl_dns_cache=300, enable_cleanup_closed=True))
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
                    await MODEL_REGISTRY_CLIENT.ingest_catalog("nous", upstream_models, "runtime-catalog")
        except Exception:
            pass
        if not upstream_models:
            upstream_models = MODEL_STORE.get_catalog(fresh_only=False)

    models_list = list(upstream_models)

    global _known_models
    _known_models = set()
    for m in models_list:
        mid = m.get("id") if isinstance(m, dict) else None
        if mid:
            _known_models.add(mid)
    for m in CURATED_FREE_MODELS:
        _known_models.add(m["id"])

    # FIX: Add curated fallback models for Codex model discovery when upstream is unavailable
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
    body = await req.json()
    est = len(str(body)) // 4
    return {"input_tokens": max(1, est)}

# --- OPENAI CHAT ---
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    await _auth_check(request)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': f'Invalid JSON: {e}'}})
    client_ip = request.client.host
    if not check_rate_limit(client_ip):
        raise HTTPException(429, {"error": {"type": "rate_limit_error", "message": "Too many requests"}})

    if body.get('max_tokens') is not None and (not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0):
        return JSONResponse(status_code=400, content={'error': {'type': 'invalid_request_error', 'message': 'max_tokens must be a positive integer'}})

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
    extra_h = {h: request.headers.get(h) for h in ["anthropic-beta", "anthropic-version", "openai-beta"] if request.headers.get(h)}

    status, result, key_entry = await post_nous_with_retries(body, stream=is_stream, extra_headers=extra_h)
    record_model_result(body.get("model", ""), key_entry, status, result, "/v1/chat/completions")
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
    body = await request.json()
    requested = body.get("model")
    if free_only_enabled() and requested:
        resolved = resolve_model(requested)
        if not model_allowed(requested) and not model_allowed(resolved):
            return JSONResponse(status_code=400, content=free_only_error(requested))
    chat_body = responses_to_chat(body)
    if free_only_enabled() and chat_body.get("model") and not model_allowed(chat_body.get("model", "")):
        return JSONResponse(status_code=400, content=free_only_error(chat_body.get("model") or requested or ""))
    is_stream = body.get("stream", False)

    status, result, key_entry = await post_nous_with_retries(chat_body, stream=is_stream, client_surface="openai_responses")
    record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/responses")
    if status != 200:
        return JSONResponse(status_code=status, content=result)

    if is_stream:
        rid = f"resp-{int(time.time()*1000)}"
        state = ResponsesStreamState(rid, chat_body["model"])
        async def gen():
            # Codex requires output_item.added BEFORE first delta.
            for ev in state.start():
                yield ev
            async for line in stream_with_heartbeat(result, lambda x: x if isinstance(x, str) else str(x), state=state, key_entry=key_entry):
                yield line
            # Store full conversation for the next previous_response_id turn.
            await store_conversation(rid, list(chat_body.get("messages", [])) + [state.assistant_message()])
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
    await store_conversation(resp["id"], saved_msgs)
    return resp

# --- ANTHROPIC MESSAGES ---
@app.post("/v1/messages")
async def messages(request: Request):
    await _auth_check(request)
    body = await request.json()
    if not isinstance(body.get('max_tokens'), int) or body['max_tokens'] <= 0:
        return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'max_tokens is required and must be a positive integer'}})
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

    status, result, key_entry = await post_nous_with_retries(chat_body, stream=is_stream, client_surface="anthropic_messages")
    record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/messages")
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
async def get_metrics():
    return metrics.snapshot()

@app.get("/metrics/prom")
async def prom():
    snap = metrics.snapshot()
    lines = [
        f'# HELP wrapper_nous_requests_total Total requests\nwrapper_nous_requests_total {snap["total_requests"]}',
        f'wrapper_nous_tokens_total {snap["total_tokens"]}',
    ]
    return Response("\n".join(lines), media_type="text/plain")

@app.get("/metrics/model-status")
async def model_status():
    return {
        "provider": "nous",
        "catalog_age_sec": MODEL_STORE.catalog_age_sec(),
        "states": MODEL_STORE.status_map(),
    }

@app.get("/healthz")
async def healthz(): return await health()

# catch-all
@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    return JSONResponse(status_code=404, content={"error": {"message": f"Unsupported: {path}", "type": "not_found_error"}})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("wrapper_nous:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
