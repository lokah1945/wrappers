#!/usr/bin/env python3
"""Key pool management for Nous Research API wrapper."""

import os
import time
import threading
import logging
from typing import Optional, Dict, Set

logger = logging.getLogger('wrapper-nous')

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
        # N-12 fix: per-model cooldowns so one broken model does not take the
        # whole key out of rotation for healthy models.
        self.model_blocked_until: Dict[str, float] = {}

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

    def block_model(self, model_id: str, seconds: int, reason: str):
        """N-12 fix: cool down only this key+model pair (model-scoped failure)."""
        if not model_id:
            return
        seconds = max(1, min(int(seconds or 1), int(os.environ.get("KEY_COOLDOWN_MAX_SEC", "300"))))
        self.model_blocked_until[model_id] = max(self.model_blocked_until.get(model_id, 0.0), time.time() + seconds)
        self.total_failures += 1
        logger.warning(f"[key_pool] Nous key {self.label} model {model_id!r} cooled down for {seconds}s ({reason})")

    def is_model_blocked(self, model_id: str) -> bool:
        if not model_id:
            return False
        until = self.model_blocked_until.get(model_id, 0.0)
        if not until:
            return False
        if time.time() < until:
            return True
        self.model_blocked_until.pop(model_id, None)
        return False

    def stats(self) -> dict:
        now = time.time()
        return {
            "label": self.label,
            "current_rpm": self.current_rpm(),
            "in_flight": self.in_flight,
            "effective_load": self.effective_load,
            "hard_blocked": self.is_blocked(),
            "hard_blocked_remaining_s": max(0, round(self.hard_blocked_until - time.time(), 1)),
            "block_reason": self.block_reason or None,
            "model_blocks": {m: round(u - now, 1) for m, u in self.model_blocked_until.items() if u > now},
            "total_requests": self.total_requests,
            "total_429s": self.total_429s,
            "total_failures": self.total_failures,
        }


class KeyPool:
    """Manages multiple Nous API keys with rotation, cooldown and in-flight tracking."""

    def __init__(self):
        self.keys: List[KeyEntry] = []
        self._lock = asyncio.Lock()
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

    def acquire(self, model_id: str = None, exclude: Optional[Set[str]] = None) -> Optional[KeyEntry]:
        """Least-loaded selection.

        N-10 fix: `exclude` lets retry loops skip labels already tried for the
        same client request. N-12 fix: keys cooled down for `model_id` only are
        skipped for that model but stay usable for other models.
        """
        with self._lock:
            candidates = [
                k for k in self.keys
                if not k.is_blocked() and k.current_rpm() < self.hard_limit
                and (not exclude or k.label not in exclude)
                and not (model_id and k.is_model_blocked(model_id))
            ]
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

    def mark_failure(self, entry: Optional[KeyEntry], status_code: int, retry_after: int = None, model_id: str = None, model_scoped: bool = False):
        if entry is None:
            return
        # N-12 fix: model-specific failures (capacity / broken model 5xx) cool
        # down only the key+model pair instead of the whole key so healthy
        # models keep rotating.
        if model_scoped and model_id:
            if status_code == 429:
                entry.block_model(model_id, retry_after or int(os.environ.get("RATE_LIMIT_COOLDOWN_SEC", "65")), "model_rate_limit")
            else:
                entry.block_model(model_id, retry_after or int(os.environ.get("TRANSIENT_KEY_COOLDOWN_SEC", "15")), "model_transient")
            return
        if status_code == 429:
            entry.block(retry_after or int(os.environ.get("RATE_LIMIT_COOLDOWN_SEC", "65")), "rate_limit")
        elif status_code in (401, 402, 403):
            entry.block(retry_after or int(os.environ.get("AUTH_KEY_COOLDOWN_SEC", "300")), "auth_or_quota")
        elif status_code >= 500 or status_code in (408, 409):
            if model_id and status_code >= 500:
                # 5xx tied to a specific model → per-model block (N-12).
                entry.block_model(model_id, retry_after or int(os.environ.get("TRANSIENT_KEY_COOLDOWN_SEC", "15")), "model_transient")
            else:
                entry.block(retry_after or int(os.environ.get("TRANSIENT_KEY_COOLDOWN_SEC", "15")), "transient")

    def heal_in_flight(self) -> int:
        """N-01 fix: reset in_flight counters stuck by leaked release paths.

        Mirrors nvidia-python's KeyPool.heal_in_flight — a key whose in_flight
        is non-zero but which has not been used for HEAL_INFLIGHT_THRESHOLD_SEC
        is assumed to have leaked its slot (e.g. an exception path that skipped
        release) and is reset so effective_load stays honest.
        """
        threshold = int(os.environ.get("HEAL_INFLIGHT_THRESHOLD_SEC", "600"))
        now = time.time()
        fixed = 0
        with self._lock:
            for k in self.keys:
                if k.in_flight > 0 and k.last_used > 0 and (now - k.last_used) > threshold:
                    logger.warning(f"[key_pool] heal_in_flight: {k.label} in_flight {k.in_flight} stuck since last_used {round(now - k.last_used)}s ago -> 0")
                    k.in_flight = 0
                    fixed += 1
                elif k.in_flight > 0 and k.last_used == 0:
                    logger.warning(f"[key_pool] heal_in_flight: {k.label} in_flight {k.in_flight} with no last_used -> 0")
                    k.in_flight = 0
                    fixed += 1
        if fixed:
            logger.info(f"[key_pool] heal_in_flight: {fixed} key(s) corrected")
        return fixed

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

try:
    from common.middleware import RequestSizeLimiter
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------
def load_dotenv(override: bool = False):
    """Parse .env files with basic support for quotes and inline comments.

    BUG-L3 fix: handle quoted values correctly, strip inline comments outside
    quotes, and skip blank/comment lines.

    N-13 fix: `override=True` replaces existing os.environ values (hot reload
    must pick up rotated keys); default keeps setdefault semantics for boot.
    """
    for p in [".env", os.path.expanduser("~/.env")]:
        if os.path.exists(p):
            with open(p) as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" not in stripped:
                        continue
                    k, v = stripped.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # Handle quoted values: strip matching outer quotes
                    if len(v) >= 2:
                        if (v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'"):
                            v = v[1:-1]
                        else:
                            # Unquoted: strip inline comment (# not inside quotes)
                            comment_idx = v.find(" #")
                            if comment_idx >= 0:
                                v = v[:comment_idx].rstrip()
                    if override:
                        os.environ[k] = v
                    else:
                        os.environ.setdefault(k, v)


if os.environ.get("WRAPPER_SKIP_DOTENV", "").lower() != "true":
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
                    # N-13 fix: reload with override so changed values (e.g.
                    # rotated keys) actually take effect, then rebuild the pool.
                    load_dotenv(override=True)
                    try:
                        KEY_POOL.load_from_env()
                    except Exception as e:
                        logger.warning(f'[env] key pool reload failed: {e}')
                    logger.info('[env] .env reloaded (hot, override)')
        obs = Observer()
        obs.schedule(EnvWatcher(), path=str(Path(__file__).parent.parent), recursive=False)
        obs.start()
        logger.info('[env] Watching .env for hot reload')
    except Exception as e:
        logger.warning(f'[env] watcher failed: {e}')

NOUS_BASE = os.environ.get("NOUS_BASE_URL", "https://inference-api.nousresearch.com").rstrip("/")
MODEL_STATE_DB = os.environ.get("MODEL_STATE_DB", str(Path(__file__).resolve().parent.parent / "model-state.db"))
MODEL_CATALOG_TTL_SEC = int(os.environ.get("MODEL_CATALOG_TTL_SEC", "21600"))
MODEL_CATALOG_REFRESH_SEC = int(os.environ.get("MODEL_CATALOG_REFRESH_SEC", "86400"))
MODEL_STORE = ModelStateStore("nous", MODEL_STATE_DB, MODEL_CATALOG_TTL_SEC)
MODEL_REGISTRY = LocalModelRegistry("nous", profile_db_path=MODEL_STATE_DB)
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
# N-06 fix: streams use a read-idle timeout instead of a hard total timeout,
# so long generations are not killed at STREAM_REQUEST_TIMEOUT_SEC and a dead
# upstream connection is detected within STREAM_SOCK_READ_TIMEOUT_SEC.
STREAM_SOCK_READ_TIMEOUT_SEC = int(os.environ.get("STREAM_SOCK_READ_TIMEOUT_SEC", "300"))
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "60"))
VERSION = "2.0.7-audit-hardening"

# Build identity (H-04/H-02): resolve git root + source root from __file__, portable
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
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=_resolve_git_root(), stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return 'unknown'

GIT_COMMIT = _resolve_git_commit()
SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))
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
    from common.logging_utils import setup_logging
    logger = setup_logging("wrapper-nous", log_file=LOG_FILE, default_log_file="/tmp/wrapper-nous.log",
                           log_format="%(asctime)s [nous] %(message)s")
except ImportError:
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




async def post_nous(payload: dict, token: str, stream: bool = False, extra_headers: dict = None) -> tuple:
    # Circuit breaker: reject if upstream is failing
    if _HAS_CIRCUIT_BREAKER:
        try:
            await _UPSTREAM_BREAKER.before_request()
        except CircuitBreakerError as cb_err:
            return 503, {"error": {"message": str(cb_err), "type": "service_unavailable"}}

    url = f"{NOUS_BASE}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if stream:
        headers["Accept"] = "text/event-stream"

    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    sess = await get_session()
    # N-01 fix: any aiohttp/network error (DNS failure, connection reset,
    # client-side timeout) is converted to a shaped 502 instead of propagating
    # out of post_nous_with_retries after KEY_POOL.acquire() incremented
    # in_flight (which permanently leaked the slot). Callers release the key
    # entry in their own finally/error paths on non-200.
    try:
        if stream:
            # IMPORTANT: Do NOT use async with for streaming — caller must release
            # N-06 fix: no hard total timeout on streams (long generations were
            # killed at 15 min); instead a sock_read idle timeout detects a
            # silently dead upstream connection quickly.
            stream_timeout = aiohttp.ClientTimeout(
                total=None,
                sock_connect=CONNECT_TIMEOUT_SEC,
                sock_read=max(30, STREAM_SOCK_READ_TIMEOUT_SEC),
            )
            resp = await sess.post(url, json=payload, headers=headers, timeout=stream_timeout)
            if resp.status != 200:
                text = await resp.text()
                resp.release()
                if _HAS_CIRCUIT_BREAKER:
                    await _UPSTREAM_BREAKER.record_failure()
                return resp.status, _normalize_upstream_error(resp.status, text)
            if _HAS_CIRCUIT_BREAKER:
                await _UPSTREAM_BREAKER.record_success()
            return 200, resp
        else:
            async with sess.post(url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    if _HAS_CIRCUIT_BREAKER:
                        await _UPSTREAM_BREAKER.record_failure()
                    return resp.status, _normalize_upstream_error(resp.status, text)
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                if _HAS_CIRCUIT_BREAKER:
                    await _UPSTREAM_BREAKER.record_success()
                return resp.status, data
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # N-15 fix: circuit breaker now actually records upstream failures.
        if _HAS_CIRCUIT_BREAKER:
            try:
                await _UPSTREAM_BREAKER.record_failure()
            except Exception:
                pass
        logger.warning(f"[upstream] post_nous network error: {type(e).__name__}: {e}")
        return 502, {"error": {"type": "api_error", "message": f"Upstream connection error: {type(e).__name__}: {str(e)[:500]}", "code": "upstream_connection_error"}}


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


def _is_retriable_upstream_status(status: int, data=None) -> bool:
    return bool(classify_upstream_error(status, data).retry_same_model)


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
            if not same_provider_model_id('nous', call_plan.model.provider_model_id, model_id):
                return 500, {"error": {"type": "server_error", "message": "Model identity changed during call-plan resolution", "code": "MODEL_ID_MUTATION"}}, None
        except ValueError as exc:
            return 400, {"error": {"type": "invalid_request_error", "message": str(exc), "code": "MODEL_CALL_PLAN_INVALID"}}, None

    last_status = 503
    last_result = {"error": {"message": "No capacity", "type": "server_error"}}
    tried = 0
    # BUG-M1 fix: preserve OAuth retry-after so it's not lost if static keys also fail
    oauth_retry_after = 0

    oauth_token = _read_token_from_auth_path()
    if oauth_token:
        status, result = await post_nous(payload, oauth_token, stream=stream, extra_headers=extra_headers)
        if status == 200:
            return status, result, None
        tried += 1
        last_status, last_result = status, result
        if status in (401, 403):
            # F8 fix: upstream rejected the cached OAuth token — drop the
            # cache so the next request re-reads AUTH_PATH from disk.
            _invalidate_auth_token_cache()
        if status == 429:
            oauth_retry_after = _retry_after_seconds(result)
        if not _is_retriable_upstream_status(status, result):
            return status, result, None

    attempts = max(1, KEY_POOL.total_keys)
    # N-10 fix: skip labels already tried for this request so acquire() cannot
    # re-select the same key every iteration, and back off with jitter between
    # attempts instead of hammering upstream back-to-back.
    tried_labels: Set[str] = set()
    retry_backoff_base = float(os.environ.get("RETRY_BACKOFF_BASE_SEC", "0.25"))
    for attempt_i in range(attempts):
        entry = KEY_POOL.acquire(model_id=model_id, exclude=tried_labels)
        if not entry:
            break
        tried_labels.add(entry.label)
        if attempt_i > 0:
            # N-10 fix: small jittered backoff between retry attempts so a down
            # model does not receive N back-to-back upstream hits per request.
            await asyncio.sleep(retry_backoff_base * attempt_i + random.uniform(0, retry_backoff_base))
        released = False
        try:
            # N-01 fix: the key's in_flight slot is always released via finally
            # (except the successful-stream case where the stream generator owns
            # the release). post_nous itself shapes network errors into a 502.
            status, result = await post_nous(payload, entry.api_key, stream=stream, extra_headers=extra_headers)
            if status == 200:
                if stream:
                    released = True  # ownership transferred to the stream generator
                    return status, result, entry
                return status, result, None
            tried += 1
            last_status, last_result = status, result
            if _is_retriable_upstream_status(status, result):
                if _should_cooldown_key(status, result):
                    KEY_POOL.mark_failure(entry, status, _retry_after_seconds(result), model_id=model_id)
                elif _looks_model_capacity_error(result) and model_id:
                    # N-12 fix: model-capacity failure blocks only this key+model.
                    KEY_POOL.mark_failure(entry, status, _retry_after_seconds(result, default=15), model_id=model_id, model_scoped=True)
                continue
            return status, result, None
        finally:
            if not released:
                KEY_POOL.release(entry)

    if tried >= max(1, KEY_POOL.total_keys + (1 if oauth_token else 0)) and isinstance(last_result, dict) and isinstance(last_result.get("error"), dict):
        msg = last_result["error"].get("message", "")
        # BUG-M1 fix: include OAuth retry-after context if it was the first failure
        oauth_hint = f" (OAuth rate-limited, retry-after={oauth_retry_after}s)" if oauth_retry_after else ""
        last_result = {"error": {**last_result["error"], "message": f"All configured Nous credentials failed or are rate-limited{oauth_hint}. Last error: {msg}"[:2000]}}
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
                if r.status in (401, 403):
                    # F8 fix: cached OAuth token rejected — force re-read.
                    _invalidate_auth_token_cache()
                last_status, last_data = r.status, _normalize_upstream_error(r.status, text)
                if not _is_retriable_upstream_status(r.status, last_data):
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
                if _is_retriable_upstream_status(r.status, last_data):
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
def responses_to_chat(body: dict, principal: str = '') -> dict:
    model = resolve_model(body.get("model"))
    msgs = []
    prev = body.get("previous_response_id")
    if prev and principal:
        stored_msgs = get_stored_conversation(principal, prev)
        if stored_msgs:
            msgs.extend(stored_msgs)

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
# N-02 fix: bound the previous_response_id store exactly like
# nvidia-python/src/responses_compat.py — FIFO cap (200 entries) plus a TTL
# prune using the stored timestamp, so long-running Codex sessions cannot leak
# memory monotonically.
_RESPONSE_STORE_MAX = int(os.environ.get("RESPONSES_STORE_MAX_ENTRIES", "200"))
_RESPONSE_STORE_TTL_SEC = int(os.environ.get("RESPONSES_STORE_TTL_SEC", "86400"))

def _prune_response_store_locked():
    """Evict expired then oldest entries. Caller must hold _STORE_LOCK."""
    now = time.time()
    if _RESPONSE_STORE_TTL_SEC > 0:
        expired = [rid for rid, (ts, _msgs) in _RESPONSE_STORE.items() if now - ts > _RESPONSE_STORE_TTL_SEC]
        for rid in expired:
            _RESPONSE_STORE.pop(rid, None)
    # dict preserves insertion order → first key is the oldest (FIFO evict)
    while len(_RESPONSE_STORE) > _RESPONSE_STORE_MAX:
        oldest = next(iter(_RESPONSE_STORE))
        _RESPONSE_STORE.pop(oldest, None)

def _extract_principal(request) -> str:
    """Extract a stable tenant identifier from the request for store namespacing.

    BUG-SEC-RESPONSE-STORE fix (2026-07-28): keys are namespaced by auth
    principal to prevent cross-tenant data leaks. Priority: Bearer token >
    x-api-key > client IP > 'anonymous'. Uses a SHA-256 fingerprint (first
    24 chars) to avoid storing raw credentials as dictionary keys.
    """
    import hashlib
    token = ''
    try:
        auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
        if auth:
            token = auth.replace("Bearer ", "", 1).strip() if auth.lower().startswith("bearer ") else auth.strip()
        if not token and request.client:
            token = request.client.host or ''
    except Exception:
        pass
    if not token:
        return 'anonymous'
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:24]

def _response_store_key(principal: str, rid: str) -> str:
    """Namespace a response ID by the caller's principal for tenant isolation."""
    return f"{principal}\x00{rid}"

async def store_conversation(principal: str, rid: str, msgs: list):
    """Store conversation history namespaced by principal (BUG-SEC-RESPONSE-STORE fix)."""
    async with _STORE_LOCK:
        # N-19 fix: store a deep copy so later in-place mutation of the live
        # message dicts cannot corrupt the stored replay history.
        try:
            msgs = copy.deepcopy(msgs)
        except (TypeError, ValueError, RecursionError):
            msgs = list(msgs)
        key = _response_store_key(principal, rid)
        _RESPONSE_STORE[key] = (time.time(), msgs)
        _prune_response_store_locked()

def get_stored_conversation(principal: str, rid: str) -> Optional[list]:
    """N-19 fix + BUG-SEC-RESPONSE-STORE fix: return a deep copy of stored
    history, namespaced by principal for tenant isolation.

    Runs synchronously on the single event loop; there is no await point
    between lookup and copy, so the read is consistent without the lock.
    """
    key = _response_store_key(principal, rid)
    stored = _RESPONSE_STORE.get(key)
    if not stored:
        return None
    try:
        return copy.deepcopy(stored[1])
    except (TypeError, ValueError, RecursionError):
        return list(stored[1])


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

    BUG-FIX: heartbeat now fires even when upstream is idle (e.g., reasoning
    models taking 10+ seconds). Previously, heartbeat only fired when upstream
    was sending data, causing Codex to time out during long reasoning steps.
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
        # N-09 fix: an empty data: payload is a valid (empty) SSE event /
        # keep-alive, NOT end-of-stream. Only literal [DONE] terminates.
        if data == b"":
            return
        if data in (b"[DONE]", b'"[DONE]"'):
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

    # BUG-FIX: heartbeats fire even when upstream is idle. N-05 fix: instead of
    # asyncio.wait_for (whose TimeoutError is indistinguishable from a genuine
    # aiohttp read/total timeout raised by __anext__), wait on a sentinel task:
    # an un-finished task after the wait window means "idle → heartbeat", while
    # a real upstream TimeoutError surfaces from task.result() and is handled
    # as an upstream error (logged, stream finalized) instead of being
    # swallowed as an idle tick.
    chunk_task = None
    client_disconnected = False
    upstream_error = None
    try:
        # Get an async iterator over chunks
        chunk_iter = upstream_resp.content.iter_any().__aiter__()
        while True:
            if chunk_task is None:
                chunk_task = asyncio.ensure_future(chunk_iter.__anext__())
            done_set, _pending = await asyncio.wait({chunk_task}, timeout=HEARTBEAT_MS / 1000)
            if not done_set:
                # No chunk within the heartbeat interval → upstream is idle.
                now = time.time()
                if now - last_hb > (HEARTBEAT_MS / 1000):
                    yield ": heartbeat\n\n"
                    last_hb = now
                continue
            finished, chunk_task = chunk_task, None
            try:
                chunk = finished.result()
            except StopAsyncIteration:
                # Stream ended normally
                break
            except asyncio.TimeoutError as e:
                # N-05 fix: genuine upstream read/total timeout — do not keep
                # heartbeating; record the cause and finalize the stream.
                upstream_error = e
                logger.warning(f"[stream] upstream timed out mid-stream ({e!r}); finalizing with synthetic terminal events")
                break
            except aiohttp.ClientError as e:
                upstream_error = e
                logger.warning(f"[stream] upstream connection error mid-stream ({type(e).__name__}: {e}); finalizing")
                break

            buffer += chunk
            # N-08 fix: tolerate CRLF SSE framing — normalize \r\n → \n before
            # splitting so a CRLF upstream still streams incrementally instead
            # of accumulating the whole response until EOF.
            if b"\r" in buffer:
                buffer = buffer.replace(b"\r\n", b"\n")
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
    except (GeneratorExit, asyncio.CancelledError):
        # N-07 fix: client disconnected — async-generator finalization must not
        # yield anything after GeneratorExit. Flag it so the finally block does
        # cleanup only.
        client_disconnected = True
        raise
    finally:
        if chunk_task is not None:
            chunk_task.cancel()
        try:
            if not terminated and not client_disconnected:
                # Flush a final partial SSE block if upstream omitted the blank line.
                tail = buffer.replace(b"\r\n", b"\n").strip()
                if tail:
                    for line in tail.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:"):
                            async for out in handle_payload(line[5:].strip()):
                                yield out
                            if terminated:
                                break
                if not terminated:
                    if upstream_error is not None:
                        # N-05 fix: surface the timeout/error cause to the client
                        # instead of a silent synthetic completion.
                        yield f": upstream-error {type(upstream_error).__name__}\n\n"
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