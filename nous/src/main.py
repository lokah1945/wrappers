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
import secrets
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

# Shared header sanitization (BUG-SEC2 fix — deduplicated from common/middleware)
try:
    # Ensure /root/wrapper (where the shared `common` package lives) is on the
    # path, since the systemd service sets PYTHONPATH=.../nous only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common.middleware import sanitize_header_value as _sanitize_header_value
except ImportError:  # noqa: E722 - fallback defined below
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
        parse_retry_after as _parse_retry_after,
        is_retriable_status as _is_retriable_status,
        should_cooldown_key as _should_cooldown_key,
        build_forward_headers as _build_forward_headers,
        sanitize_header_value as _sanitize_header_value,
        anthropic_to_openai_response,
        openai_to_anthropic_response,
        stream_anthropic_to_openai,
        openai_chat_to_anthropic_request,
        new_response_id as _new_response_id,
    )
    from common.compat import (
        is_anthropic_upstream as _is_anthropic_upstream,
        resolve_upstream_is_anthropic as _resolve_upstream_is_anthropic,
        passthrough_anthropic_sse as _passthrough_anthropic_sse,
        translate_anthropic_stream_to_openai_chat as _translate_anthropic_stream_to_openai_chat,
        translate_openai_chat_sse_to_responses as _translate_openai_chat_sse_to_responses,
    )
    _USING_SHARED_TRANSLATIONS = True
except ImportError:
    _USING_SHARED_TRANSLATIONS = False

    def _new_response_id(prefix: str = "resp") -> str:  # type: ignore[misc]
        import secrets as _s
        return f"{prefix}_{int(time.time() * 1000)}-{_s.token_hex(6)}"

async def _upstream_is_anthropic() -> bool:
    # R17 (B-17.1): defer the dialect routing decision to the shared
    # resolver so COMPATIBILITY_LAYER=3 auto-discovery actually applies.
    if _USING_SHARED_TRANSLATIONS:
        return await _resolve_upstream_is_anthropic(get_session, NOUS_BASE)
    return _is_anthropic_upstream()


# P0-4 fix (audit 2026-08-03): central special-token scrubbing. Byte-level-BPE
# upstream models leak tokenizer control tokens (<unk> — often fragmented
# across SSE chunks as '<un' + 'k>' — <s>, </s>, <|im_start|>, U+0800 …) into
# content and reasoning streams; they arrived verbatim in Claude Code output
# (user report: '"><unk><unk><unk>…"'). One filter per visible channel.
try:
    from common.sanitize_tokens import (
        SpecialTokenFilter as _SpecialTokenFilter,
        DsmlMarkupFilter as _DsmlMarkupFilter,
        filter_special_tokens as _filter_special_tokens,
        strip_dsml_markup as _strip_dsml_markup,
        scrub_openai_response_inplace as _scrub_openai_response_inplace,
        scrub_chat_chunk_inplace as _scrub_chat_chunk_inplace,
        flushed_deltas as _flushed_deltas,
    )
except ImportError:  # pragma: no cover - standalone fallback
    class _SpecialTokenFilter:  # type: ignore[no-redef]
        def feed(self, t):
            return t

        def flush(self):
            return ''

    class _DsmlMarkupFilter:  # type: ignore[no-redef]
        collected_text = ''

        def feed(self, t):
            return t

        def flush(self):
            return ''

    def _filter_special_tokens(t):  # type: ignore[misc]
        return t

    def _strip_dsml_markup(t):  # type: ignore[misc]
        return t

    def _scrub_openai_response_inplace(d):  # type: ignore[misc]
        return None

    def _scrub_chat_chunk_inplace(o, ft, fr, dsml=None):  # type: ignore[misc]
        return None

    def _flushed_deltas(ft, fr):  # type: ignore[misc]
        return '', ''

# P1-1/P1-3 shared helpers (input_image passthrough, full Responses usage).
try:
    from common.translations.shared import (
        responses_content_to_chat as _responses_content_to_chat,
        tokens_from_chat_usage as _tokens_from_chat_usage,
        responses_usage as _responses_usage,
    )
except ImportError:  # pragma: no cover - standalone fallback
    def _responses_content_to_chat(c):  # type: ignore[misc]
        if isinstance(c, list):
            return " ".join(p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") in ("input_text", "text", "output_text"))
        return c

    def _tokens_from_chat_usage(u):  # type: ignore[misc]
        u = u if isinstance(u, dict) else {}
        return (u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0, 0, 0)

    def _responses_usage(i=0, o=0, c=0, r=0):  # type: ignore[misc]
        return {"input_tokens": int(i or 0), "output_tokens": int(o or 0),
                "total_tokens": int(i or 0) + int(o or 0)}


# ============================================================================
# KeyPool for multi-key rotation (parity with opencode/nvidia-python)
# ============================================================================

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
        """B-37 fix: side-effect-free predicate (see blackbox key_pool)."""
        return time.time() < self.hard_blocked_until

    def expire_block(self) -> None:
        """Clear an elapsed hard block. Caller must hold the pool lock."""
        if self.hard_blocked_until and time.time() >= self.hard_blocked_until:
            self.hard_blocked_until = 0.0
            self.block_reason = ""

    def record(self):
        """B-36 fix: telemetry only; in-flight accounting is now explicit."""
        now = time.time()
        self.timestamps.append(now)
        self.total_requests += 1
        self.last_used = now

    def increment_in_flight(self):
        self.in_flight += 1

    def decrement_in_flight(self):
        if self.in_flight > 0:
            self.in_flight -= 1

    def release(self):
        # Backwards-compatible alias.
        self.decrement_in_flight()

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
        """Side-effect-free model-block predicate (B-37 parity)."""
        if not model_id:
            return False
        until = self.model_blocked_until.get(model_id, 0.0)
        if not until:
            return False
        return time.time() < until

    def expire_model_blocks(self) -> None:
        """Clear elapsed model-scoped blocks. Caller must hold the pool lock."""
        now = time.time()
        for model_id, until in list(self.model_blocked_until.items()):
            if until <= now:
                self.model_blocked_until.pop(model_id, None)

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
        # B-38 fix: asyncio.Lock in async request paths. The old threading.Lock
        # blocked the event loop and diverged from every sibling pool.
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

    async def acquire(self, model_id: str = None, exclude: Optional[Set[str]] = None) -> Optional[KeyEntry]:
        """Least-loaded selection.

        N-10 fix: `exclude` lets retry loops skip labels already tried for the
        same client request. N-12 fix: keys cooled down for `model_id` only are
        skipped for that model but stay usable for other models.
        """
        async with self._lock:
            # B-37: expire elapsed blocks explicitly, under the lock.
            for k in self.keys:
                k.expire_block()
                k.expire_model_blocks()
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
            # B-36: telemetry and in-flight accounting are now explicit.
            entry.record()
            entry.increment_in_flight()
            return entry

    async def release(self, entry: Optional[KeyEntry]):
        if entry is None:
            return
        async with self._lock:
            entry.decrement_in_flight()

    async def mark_failure(self, entry: Optional[KeyEntry], status_code: int, retry_after: int = None, model_id: str = None, model_scoped: bool = False):
        if entry is None:
            return
        async with self._lock:
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

    async def heal_in_flight(self) -> int:
        """N-01 fix: reset in_flight counters stuck by leaked release paths.

        Mirrors nvidia-python's KeyPool.heal_in_flight — a key whose in_flight
        is non-zero but which has not been used for HEAL_INFLIGHT_THRESHOLD_SEC
        is assumed to have leaked its slot (e.g. an exception path that skipped
        release) and is reset so effective_load stays honest.
        """
        threshold = int(os.environ.get("HEAL_INFLIGHT_THRESHOLD_SEC", "600"))
        now = time.time()
        fixed = 0
        async with self._lock:
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

    async def peek(self) -> Optional[KeyEntry]:
        async with self._lock:
            for k in self.keys:
                k.expire_block()
                k.expire_model_blocks()
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

    def prom_metrics(self) -> str:
        """R14 fix (CONTRACT §10 parity): Prometheus exposition for the pool —
        opencode/blackbox/openrouter/nvidia all expose pool-level series from
        /metrics/prom; nous (inlined-pool deviation) emitted only 3 hardcoded
        counters. Port the sibling format verbatim with nous_ metric names."""
        lines = [
            '# HELP nous_keys_total Total keys',
            '# TYPE nous_keys_total gauge',
            f'nous_keys_total {self.total_keys}',
            '# HELP nous_keys_available Available keys',
            '# TYPE nous_keys_available gauge',
            f'nous_keys_available {self.available_keys}',
            '# HELP nous_in_flight_total In flight',
            '# TYPE nous_in_flight_total gauge',
            f'nous_in_flight_total {sum(k.in_flight for k in self.keys)}',
        ]
        for k in self.keys:
            st = k.stats()
            lines.append(f'nous_key_rpm{{key="{k.label}"}} {st["current_rpm"]}')
            lines.append(f'nous_key_blocked{{key="{k.label}"}} {1 if st["hard_blocked"] else 0}')
            lines.append(f'nous_key_failures_total{{key="{k.label}"}} {st["total_failures"]}')
        return '\n'.join(lines) + '\n'

# ============================================================================

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

try:
    from common.middleware import RequestSizeLimiter
    _HAS_SIZE_LIMITER = True
except ImportError:
    _HAS_SIZE_LIMITER = False

# B-28/B-29/B-30 fix: shared, fail-closed auth (see common/auth.py).
try:
    from common.auth import check_auth as _shared_check_auth
    _HAS_SHARED_AUTH = True
except ImportError:  # pragma: no cover
    _HAS_SHARED_AUTH = False

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
RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "600"))
VERSION = "2.0.7-audit-hardening"

# Build identity (H-04/H-02): resolve git root + source root from __file__, portable
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
        return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=_resolve_git_root(), stderr=subprocess.DEVNULL, timeout=3).decode().strip()
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
    """True if model name/id has a free suffix (case-insensitive).

    Optional FREE_MODEL_ALLOWLIST=comma,separated,ids for free models whose
    ids do not carry a :free/-free suffix (e.g. niche upstream names).
    """
    if not model_id:
        return False
    mid = str(model_id).lower().strip()
    bare = mid.split("/")[-1] if "/" in mid else mid
    if mid.endswith((":free", "-free")) or bare.endswith((":free", "-free")):
        return True
    allow = (os.environ.get("FREE_MODEL_ALLOWLIST") or "").strip()
    if not allow:
        return False
    extras = {x.strip().lower() for x in allow.split(",") if x.strip()}
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
# DYNAMIC ALIASES — operator-configured name resolution (NOT model fallback)
# --------------------------------------------------------------------------
# Virtual Claude Code / Anthropic short names. They NEVER point to a fixed model.
# The operator sets DYNAMIC_ALIAS_TARGET env var at startup to bind all aliases
# to a concrete model id. Concrete client requests are forwarded verbatim and
# NEVER mutate alias state.
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
    """Transparent pass-through + operator-configured alias resolution.

    - Concrete id → pass through unchanged (NEVER mutates alias state).
    - Alias (sonnet/haiku/...) → operator-configured DYNAMIC_ALIAS_TARGET if
      bound at startup; else pass through unchanged.
    - Never inject DEFAULT_MODEL / REASONING_MODEL as a hidden default.
    - NO MODEL FALLBACK: the model id is never changed based on availability
      or error. Alias resolution is name resolution, not substitution.
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




async def post_nous(payload: dict, token: str, stream: bool = False, extra_headers: dict = None,
                     path: str = 'v1/chat/completions') -> tuple:
    """Transparent proxy: forward chat/completions to Nous upstream.

    On 429, parses the HTTP Retry-After header and embeds it in the error
    dict so post_nous_with_retries can cool down the key for the correct
    duration (anti rate-limit).
    """
    url = f"{NOUS_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept-Encoding": "gzip, deflate"}
    if stream:
        headers["Accept"] = "text/event-stream"

    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})

    sess = await get_session()
    try:
        if stream:
            stream_timeout = aiohttp.ClientTimeout(
                total=None,
                sock_connect=CONNECT_TIMEOUT_SEC,
                sock_read=max(30, STREAM_SOCK_READ_TIMEOUT_SEC),
            )
            resp = await sess.post(url, json=payload, headers=headers, timeout=stream_timeout)
            if resp.status != 200:
                text = await resp.text()
                # Parse Retry-After header for 429 cooldown (anti rate-limit).
                retry_after = _parse_retry_after(resp.headers, None) if resp.status == 429 else 0
                resp.release()
                try:
                    data = json.loads(text) if text else text
                except Exception:
                    data = text
                err = _normalize_upstream_error(resp.status, data)
                if retry_after and isinstance(err, dict):
                    err.setdefault('error', {})['retry_after'] = retry_after
                return resp.status, err
            return 200, resp
        else:
            async with sess.post(url, json=payload, headers=headers) as resp:
                text = await resp.text()
                if resp.status != 200:
                    # Parse Retry-After header for 429 cooldown (anti rate-limit).
                    try:
                        body_data = json.loads(text) if text else text
                    except Exception:
                        body_data = text
                    retry_after = _parse_retry_after(resp.headers, body_data if isinstance(body_data, dict) else None) if resp.status == 429 else 0
                    err = _normalize_upstream_error(resp.status, body_data)
                    if retry_after and isinstance(err, dict):
                        err.setdefault('error', {})['retry_after'] = retry_after
                    return resp.status, err
                try:
                    data = json.loads(text) if text else {}
                except Exception:
                    data = {"error": {"message": text[:2000], "type": "api_error"}}
                return resp.status, data
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[upstream] post_nous network error: {type(e).__name__}: {e}")
        return 502, {"error": {"type": "api_error", "message": f"Upstream connection error: {type(e).__name__}: {str(e)[:500]}", "code": "upstream_connection_error"}}


def _retry_after_seconds(data, default=65) -> int:
    """Delegate to shared parse_retry_after. Kept for backward compat."""
    if isinstance(data, tuple) and len(data) == 2:
        headers, body = data
        return _parse_retry_after(headers, body, default)
    return _parse_retry_after(None, data if isinstance(data, dict) else None, default)


def _is_retriable_upstream_status(status: int, data=None) -> bool:
    return bool(classify_upstream_error(status, data).retry_same_model)


def _looks_model_capacity_error(data) -> bool:
    blob = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
    return any(x in blob for x in ('no deployments available', 'selected model', 'cooldown_list', 'invalid model name', 'model unavailable'))


# B-21 fix: the local `_should_cooldown_key` that used to live here SHADOWED
# the `should_cooldown_key` imported from common.translations, so cooldown
# policy silently diverged per wrapper — exactly the drift
# CROSS_WRAPPER_BUG_POLICY.md exists to prevent. The model-capacity carve-out
# has been promoted into the shared implementation; this module now uses it
# directly (imported above as _should_cooldown_key).


async def post_nous_with_retries(payload: dict, stream: bool = False, extra_headers: dict = None,
                                   client_surface: str = "openai_chat", path: str = "v1/chat/completions") -> tuple:
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

    last_status = 429
    last_result = {"error": {"message": "No capacity — all keys exhausted or rate-limited", "type": "rate_limit_error"}}
    tried = 0
    # BUG-M1 fix: preserve OAuth retry-after so it's not lost if static keys also fail
    oauth_retry_after = 0

    oauth_token = _read_token_from_auth_path()
    if oauth_token:
        status, result = await post_nous(payload, oauth_token, stream=stream, extra_headers=extra_headers, path=path)
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
        entry = await KEY_POOL.acquire(model_id=model_id, exclude=tried_labels)
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
            status, result = await post_nous(payload, entry.api_key, stream=stream, extra_headers=extra_headers, path=path)
            if status == 200:
                if stream:
                    released = True  # ownership transferred to the stream generator
                    return status, result, entry
                return status, result, None
            tried += 1
            last_status, last_result = status, result
            if _is_retriable_upstream_status(status, result):
                if _should_cooldown_key(status, result):
                    await KEY_POOL.mark_failure(entry, status, _retry_after_seconds(result), model_id=model_id)
                elif _looks_model_capacity_error(result) and model_id:
                    # N-12 fix: model-capacity failure blocks only this key+model.
                    await KEY_POOL.mark_failure(entry, status, _retry_after_seconds(result, default=15), model_id=model_id, model_scoped=True)
                continue
            return status, result, None
        finally:
            if not released:
                await KEY_POOL.release(entry)

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
    last_status = 429
    last_data = {"error": {"message": "No capacity — all keys exhausted or rate-limited", "type": "rate_limit_error"}}
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
        entry = await KEY_POOL.acquire()
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
                    await KEY_POOL.release(entry)
                    return r.status, data
                last_status, last_data = r.status, _normalize_upstream_error(r.status, text)
                if _is_retriable_upstream_status(r.status, last_data):
                    if _should_cooldown_key(r.status, last_data):
                        await KEY_POOL.mark_failure(entry, r.status, _retry_after_seconds(last_data))
                    await KEY_POOL.release(entry)
                    continue
                await KEY_POOL.release(entry)
                return last_status, last_data
        except Exception as e:
            await KEY_POOL.mark_failure(entry, 503, 15)
            await KEY_POOL.release(entry)
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
    """CONTRACT §7 (no forking): delegate to the SHARED implementation when
    common.translations is importable (production). The local body only runs
    in the documented ImportError fallback path (isolated tooling) — the
    previous local copy had drifted (list-typed tool content stringified as
    raw JSON instead of joining text blocks)."""
    if _USING_SHARED_TRANSLATIONS:
        return _repair_orphan_tool_messages(messages)
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
            _c = m.get('content', '')
            if isinstance(_c, list):
                _c = " ".join(b.get("text", "") for b in _c
                              if isinstance(b, dict) and b.get("type") == "text")
            out.append({"role": "user", "content": f"Tool result{(' for ' + tcid) if tcid else ''}: {_c}"})
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
            if t == 'reasoning':
                continue  # multi-turn Codex input includes reasoning items; chat has no placeholder
            if t == "function_call_output":
                outv = it.get("output", "")
                msgs.append({"role": "tool", "tool_call_id": it.get("call_id"),
                             "content": outv if isinstance(outv, str) else json.dumps(outv, ensure_ascii=False)})
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
                # P1-1 fix: input_image parts were silently dropped here —
                # the shared helper converts them to OpenAI image_url parts.
                c = _responses_content_to_chat(c)
                msgs.append({"role": role, "content": c})

    if body.get("instructions"):
        if msgs and msgs[0].get("role") == "system":
            msgs[0]["content"] = body["instructions"] + "\n\n" + msgs[0].get("content", "")
        else:
            msgs.insert(0, {"role": "system", "content": body["instructions"]})

    msgs = repair_orphan_tool_messages(msgs)
    out = {"model": model, "messages": msgs, "stream": body.get("stream", False)}
    # TRANSPARENT PROXY: only set max_tokens if client explicitly sent one.
    # Never inject a default or enforce a minimum (was max(..., 1024) and
    # default 4096) — that mutates client intent.
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    elif body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]
    # Forward all client params verbatim (transparent proxy).
    for k in ("temperature", "top_p", "tool_choice", "stop", "seed",
              "parallel_tool_calls", "stream_options", "user", "metadata",
              "frequency_penalty", "presence_penalty", "logit_bias",
              "logprobs", "top_logprobs", "response_format", "service_tier"):
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
# P2 fix (audit 2026-08-03): WRAPPER_CONTRACT §6.3 requires the store bounded
# on THREE axes — entries, BYTES, and TTL (defaults 200 / 32MiB / 3600s).
# nous previously had no byte bound and an 86400s TTL: 200 multi-MB Codex
# histories were still unbounded in RAM and stale histories lived a full day.
_RESPONSE_STORE_MAX = int(os.environ.get("RESPONSES_STORE_MAX_ENTRIES", "200"))
_RESPONSE_STORE_TTL_SEC = int(os.environ.get("RESPONSES_STORE_TTL_SEC", "3600"))
_RESPONSE_STORE_MAX_BYTES = int(os.environ.get("RESPONSES_STORE_MAX_BYTES", str(32 * 1024 * 1024)))

def _prune_response_store_locked():
    """Evict expired then oldest entries until within all bounds.
    Caller must hold _STORE_LOCK. Entries are (ts, size_bytes, msgs)."""
    now = time.time()
    if _RESPONSE_STORE_TTL_SEC > 0:
        expired = [rid for rid, entry in _RESPONSE_STORE.items()
                   if isinstance(entry, tuple) and now - entry[0] > _RESPONSE_STORE_TTL_SEC]
        for rid in expired:
            _RESPONSE_STORE.pop(rid, None)
    # dict preserves insertion order → first key is the oldest (FIFO evict)
    while len(_RESPONSE_STORE) > _RESPONSE_STORE_MAX:
        oldest = next(iter(_RESPONSE_STORE))
        _RESPONSE_STORE.pop(oldest, None)
    # Byte budget: evict oldest-first until the total fits (never the entry
    # just written unless it alone exceeds the budget — store_conversation
    # rejects oversized histories up front).
    total = sum(e[1] for e in _RESPONSE_STORE.values() if isinstance(e, tuple) and len(e) == 3)
    while total > _RESPONSE_STORE_MAX_BYTES and len(_RESPONSE_STORE) > 1:
        oldest = next(iter(_RESPONSE_STORE))
        entry = _RESPONSE_STORE.pop(oldest, None)
        if isinstance(entry, tuple) and len(entry) == 3:
            total -= entry[1]

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
        # P2 fix: track the payload size so the store honours the byte budget.
        try:
            size = len(json.dumps(msgs, ensure_ascii=False))
        except (TypeError, ValueError):
            size = 0
        if size > _RESPONSE_STORE_MAX_BYTES:
            logger.warning(f"[responses] history for {rid} too large ({size}B); not stored")
            return
        _RESPONSE_STORE[key] = (time.time(), size, msgs)
        _prune_response_store_locked()

def get_stored_conversation(principal: str, rid: str) -> Optional[list]:
    """N-19 fix + BUG-SEC-RESPONSE-STORE fix: return a deep copy of stored
    history, namespaced by principal for tenant isolation.

    Runs synchronously on the single event loop; there is no await point
    between lookup and copy, so the read is consistent without the lock.
    Entries are (ts, size_bytes, msgs) — tolerate the legacy 2-tuple shape.
    """
    key = _response_store_key(principal, rid)
    stored = _RESPONSE_STORE.get(key)
    if not stored:
        return None
    msgs = stored[-1]
    try:
        return copy.deepcopy(msgs)
    except (TypeError, ValueError, RecursionError):
        return list(msgs)


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
    # P0-4: scrub special tokens from non-stream visible channels too.
    # R5 audit: strip DSML tool markup from the visible output text too.
    text = _strip_dsml_markup(_filter_special_tokens(msg.get("content") or ""))
    tool_calls = msg.get("tool_calls") or []
    output = []
    # B18 parity: surface upstream reasoning_content as a reasoning output
    # item (opencode/blackbox/openrouter parity) instead of dropping it.
    reasoning = _filter_special_tokens(msg.get("reasoning_content") or msg.get("reasoning") or "")
    if reasoning:
        # CODEX-RESP-02: the SDK's ResponseReasoningItem expects
        # summary/content as lists — `text` alone parses with serializer
        # warnings in the openai SDK.
        output.append({"id": f"rsn_{int(time.time()*1000)}", "type": "reasoning",
                       "status": "completed", "summary": [],
                       "content": [{"type": "reasoning_text", "text": reasoning}]})
    for tc in tool_calls:
        fn = tc.get("function", {})
        output.append({"id": tc.get("id"), "type": "function_call", "call_id": tc.get("id"), "name": fn.get("name"), "arguments": fn.get("arguments", ""), "status": "completed"})
    # P1-3 fix (SDK-strict): the message item needs a `status` and output_text
    # parts require an `annotations` array — strict Response parsing failed
    # without them.
    output.append({"id": "msg-local", "type": "message", "status": "completed", "role": "assistant",
                   "content": [{"type": "output_text", "text": text, "annotations": []}]})
    # P1-3 fix: the Responses usage object requires the *_details structures.
    _in, _out, _cached, _rsn = _tokens_from_chat_usage(chat.get("usage"))
    # CODEX-RESP-02: the openai SDK's Response model REQUIRES top-level
    # parallel_tool_calls / tool_choice / tools — a response missing them
    # fails non-streaming client.responses.create() parsing.
    # R7 concurrency: ALWAYS mint a fresh unique id — reusing the upstream
    # chat completion id (or an ms timestamp) collides across concurrent
    # turns and agents then replayed each other's stored history.
    return {
        "id": _new_response_id(),
        "object": "response", "created_at": int(time.time()), "model": model,
        "output": output, "status": "completed",
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "usage": _responses_usage(_in, _out, _cached, _rsn),
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
                if src.get("type") == "base64":
                    url = f"data:{src.get('media_type','image/png')};base64,{src.get('data','')}"
                else:
                    url = src.get("url", "")
                if url:
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            elif bt == "thinking": reasoning.append(b.get("thinking", ""))
            elif bt == "tool_use":
                tools.append({"id": b.get("id"), "type": "function", "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input", {}))}})
            elif bt == "tool_result":
                tc = b.get("tool_use_id")
                rc = b.get("content")
                txt = rc if isinstance(rc, str) else "\n".join(x.get("text","") for x in rc if isinstance(x, dict))
                msgs.append({"role": "tool", "tool_call_id": tc, "content": txt})

        # NB-7/DR-5 parity (opencode): a single non-text part (e.g. one image
        # block) must be wrapped in a list — indexing parts[0]["text"] on an
        # image_url part raised KeyError, crashing every vision request that
        # sent exactly one image with no accompanying text; and OpenAI chat
        # content must be a string or an ARRAY, never a bare part dict.
        final_c = parts if len(parts) > 1 else (
            parts[0]["text"] if parts and parts[0].get("type") == "text" else ([parts[0]] if parts else None))
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
    # TRANSPARENT PROXY: only set max_tokens if client explicitly sent one.
    # Never inject a default or enforce a minimum (was max(..., 1024) and
    # default 4096) — that mutates client intent.
    if req.get("max_tokens") is not None:
        out["max_tokens"] = req["max_tokens"]
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
        if req.get(src) is not None: out[dst] = req[src]
    # Anthropic tool_choice → OpenAI tool_choice (parity with blackbox /
    # openrouter). Anthropic sends {"type":"auto"|"any"|"tool","name":...};
    # OpenAI expects "auto"/"required"/"none" or a function-choice object.
    tc = req.get("tool_choice")
    if tc is not None:
        if isinstance(tc, dict):
            t = tc.get("type")
            if t == "auto":
                out["tool_choice"] = "auto"
            elif t == "any":
                out["tool_choice"] = "required"
            elif t == "tool" and tc.get("name"):
                out["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
            else:
                out["tool_choice"] = tc
        else:
            out["tool_choice"] = tc
    if req.get("tools"):
        out["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": normalize_schema(t.get("input_schema", {}))}} for t in req["tools"] if t.get("name")]
    return out




def openai_to_anthropic(model: str, chat: dict) -> dict:
    """OpenAI chat completion → Anthropic message (Claude Code native blocks)."""
    if isinstance(chat, dict) and chat.get("type") == "message" and "content" in chat:
        return chat

    msg = (chat.get("choices") or [{}])[0].get("message", {}) or {}
    text = msg.get("content") or ""
    if text is None:
        text = ""
    # P0-4: scrub special tokens from visible channels.
    reasoning = _filter_special_tokens(msg.get("reasoning_content") or msg.get("reasoning") or "")

    content = []
    # Keep thinking SEPARATE — never concatenate into text (Claude Code contract)
    if reasoning:
        # P1-2: thinking blocks require a `signature` field (strict SDK parse).
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})

    tool_calls = list(msg.get("tool_calls") or [])
    dsml_tools = []
    if isinstance(text, str) and "DSML" in text.replace("\uff5c", "|"):
        text, dsml_tools = _parse_dsml_from_text(text)
    text = _filter_special_tokens(text) if isinstance(text, str) else text

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
            "id": tc.get("id") or f"toolu_{int(time.time()*1000)}-{secrets.token_hex(3)}",
            "name": fn.get("name") or "",
            "input": inp if isinstance(inp, dict) else {"value": inp},
        })
    content.extend(dsml_tools)

    if not content:
        content.append({"type": "text", "text": ""})

    u = chat.get("usage", {}) or {}
    fr = (chat.get("choices") or [{}])[0].get("finish_reason")
    stop_map = {"tool_calls": "tool_use", "stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}
    if fr is not None:
        # B-06 parity for non-streaming replies: respect the explicit
        # finish_reason even if the response contains tool calls/DSML tools.
        stop_reason = stop_map.get(fr, "end_turn")
    else:
        # Only infer tool_use when the upstream omitted finish_reason entirely.
        stop_reason = "tool_use" if (tool_calls or dsml_tools) else "end_turn"
    # R5 audit (shared-translator parity): DSML markup is the ONLY tool-call
    # signal MiniMax emits — the turn reports finish 'stop'. With recovered
    # tool_use blocks in content, end_turn would make the agent close the
    # turn and never execute the tool. Stream paths already upgrade.
    if dsml_tools and stop_reason == "end_turn":
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
def _responses_sse_serialize(x):
    """B-11 fix: SSE serializer for the Responses surface.

    Guarantees a well-formed frame for any event shape. The previous
    `lambda x: x if isinstance(x, str) else str(x)` emitted a Python repr for
    dict events — single-quoted, non-JSON — which Codex cannot parse and may
    surface as raw text.
    """
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        # Support both {"type":..., "data":{...}} and a bare event payload.
        etype = x.get('type')
        data = x.get('data') if isinstance(x.get('data'), dict) else x
        if etype and 'type' not in data:
            data = {**data, 'type': etype}
        if etype:
            return f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    logger.warning(f"[responses] unexpected SSE event type {type(x).__name__}; dropping")
    return ""


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
    # P0-1: track whether the upstream signalled a NATURAL end of turn — a
    # finish_reason (or an upstream error frame). [DONE] alone sets
    # `terminated`. EOF with neither is a premature close and MUST surface as
    # an error (WRAPPER_CONTRACT §3.3), not a fabricated success.
    saw_finish = False
    # P0-4: scrubbing for the raw passthrough surface (state=None) — one
    # filter pair per stream so tokens fragmented across chunks are removed.
    _pt_text = _SpecialTokenFilter()
    _pt_reason = _SpecialTokenFilter()
    # R5 audit: DSML markup suppression on the raw passthrough text channel.
    _pt_dsml = _DsmlMarkupFilter()
    _pt_scaffold = {"id": "chatcmpl-proxy", "created": int(time.time()), "model": ""}

    def _pt_flush_chunk():
        """Final passthrough delta carrying text withheld by the filters."""
        _ft = _pt_text.feed(_pt_dsml.flush())
        _ft2, _fr = _flushed_deltas(_pt_text, _pt_reason)
        _ft = (_ft or '') + (_ft2 or '')
        _d = {}
        if _ft:
            _d["content"] = _ft
        if _fr:
            _d["reasoning_content"] = _fr
        if not _d:
            return None
        return {"id": _pt_scaffold["id"], "object": "chat.completion.chunk",
                "created": _pt_scaffold["created"], "model": _pt_scaffold["model"],
                "choices": [{"index": 0, "delta": _d, "finish_reason": None}]}

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
        nonlocal terminated, saw_finish, mid_fault
        # N-09 fix: an empty data: payload is a valid (empty) SSE event /
        # keep-alive, NOT end-of-stream. Only literal [DONE] terminates.
        if data == b"":
            return
        if data in (b"[DONE]", b'"[DONE]"'):
            # P0-4: release withheld tail text before the terminator.
            if state is None:
                _fl = _pt_flush_chunk()
                if _fl is not None:
                    yield f"data: {json.dumps(_fl, ensure_ascii=False)}\n\n"
            # P0-1 parity (CONTRACT §3.3): a healthy OpenAI stream always
            # carries finish_reason BEFORE [DONE]; a bare [DONE] is a
            # truncation signal from a middlebox. The other four wrappers now
            # surface an error in this case — nous must too instead of
            # fabricating a clean completion (agent "stops mid-run").
            _done_premature = ("upstream stream ended with [DONE] but no "
                               "finish_reason; the response may be truncated — "
                               "client may retry")
            if state is not None and hasattr(state, "translate_chunk"):
                _st_terminal = (
                    getattr(state, "finished", False)
                    or getattr(state, "_finished", False)
                    or getattr(state, "_completed", False)
                    or getattr(state, "upstream_error", None)
                )
                if not _st_terminal:
                    for ev in state.translate_chunk(
                            {"error": {"type": "api_error", "message": _done_premature}}):
                        if isinstance(ev, str):
                            yield ev
                        else:
                            yield serialize_fn(ev) if callable(serialize_fn) else ev
            elif state is None and not saw_finish:
                mid_fault = True
                yield f"data: {json.dumps({'error': {'type': 'api_error', 'code': 'upstream_done_without_finish', 'message': _done_premature}}, ensure_ascii=False)}\n\n"
            async for ev in emit_state_done():
                yield ev
            if state is None:
                yield "data: [DONE]\n\n"
            terminated = True
            return
        try:
            parsed = json.loads(data)
        except Exception:
            # B-10 fix (CRITICAL): NEVER synthesise assistant content from a
            # frame we failed to parse. The old fallback wrapped the raw line
            # as {"delta": {"content": <raw bytes>}}, so when the upstream (or
            # any relay) spoke Anthropic SSE on this surface the wrapper
            # re-emitted `event: content_block_stop` / `data: {...}` as
            # text_delta — i.e. protocol frames were printed to the user as
            # model prose in Claude Code. Log and drop, matching the
            # opencode/blackbox/openrouter behaviour.
            _preview = data[:200].decode('utf-8', errors='replace')
            logger.warning(f"[stream] dropping unparsable SSE frame ({len(data)}B): {_preview!r}")
            return

        if state and hasattr(state, "translate_chunk"):
            for ev in state.translate_chunk(parsed):
                if isinstance(ev, str):
                    yield ev
                else:
                    yield serialize_fn(ev) if callable(serialize_fn) else ev
        else:
            if isinstance(parsed, dict):
                # P0-1: remember whether a natural terminal was seen.
                _ch0 = ((parsed.get("choices") or [None])[0])
                if (isinstance(_ch0, dict) and _ch0.get("finish_reason")) \
                        or parsed.get("error") is not None:
                    saw_finish = True
                # P0-4: scrub visible text/reasoning through the cross-chunk
                # filters before forwarding (user report: '<unk><unk>…').
                # R5: also suppress DSML tool markup on the text channel.
                _scrub_chat_chunk_inplace(parsed, _pt_text, _pt_reason, dsml=_pt_dsml)
                for _k in ("id", "model", "created"):
                    if parsed.get(_k):
                        _pt_scaffold[_k] = parsed[_k]
            # B-10 fix (second leak path): re-serialise the PARSED object rather
            # than echoing raw upstream bytes, so a non-OpenAI frame can never
            # be forwarded verbatim onto an OpenAI-SSE surface.
            yield f"data: {json.dumps(parsed, ensure_ascii=False)}\n\n"

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
    # B-39 parity (CONTRACT §10): a mid-stream fault must be visible in the
    # error counters — the stream started with HTTP 200, so `status != 200`
    # accounting at the call site forever reports these as successful turns.
    mid_fault = False
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
                    # P0-1 (WRAPPER_CONTRACT §3.3): the upstream connection
                    # CLOSED without a terminal signal — no finish_reason, no
                    # error frame, no [DONE]. The old code fabricated a clean
                    # completion here (end_turn / response.completed), so a
                    # truncated answer persisted as a successful turn and the
                    # agent saw the run "stop half-way" with no way to detect
                    # or retry it. Surface a real error event instead; the
                    # terminal events still follow so no client hangs.
                    _premature = (
                        f"upstream stream ended prematurely ({type(upstream_error).__name__}); "
                        "the response may be truncated — client may retry"
                        if upstream_error is not None else
                        "upstream stream ended prematurely: EOF without finish_reason or [DONE]; "
                        "the response may be truncated — client may retry"
                    )
                    if state is not None and hasattr(state, "translate_chunk"):
                        _st_terminal = (
                            getattr(state, "finished", False)
                            or getattr(state, "_finished", False)
                            or getattr(state, "_completed", False)
                            or getattr(state, "upstream_error", None)
                        )
                        if not _st_terminal:
                            for ev in state.translate_chunk(
                                    {"error": {"type": "api_error", "message": _premature}}):
                                if isinstance(ev, str):
                                    yield ev
                                else:
                                    yield serialize_fn(ev) if callable(serialize_fn) else ev
                    elif state is None and not saw_finish:
                        _fl = _pt_flush_chunk()
                        if _fl is not None:
                            yield f"data: {json.dumps(_fl, ensure_ascii=False)}\n\n"
                        yield f"data: {json.dumps({'error': {'type': 'api_error', 'code': 'upstream_premature_eof', 'message': _premature}}, ensure_ascii=False)}\n\n"
                        mid_fault = True
                    async for ev in emit_state_done():
                        yield ev
                    if state is None:
                        yield "data: [DONE]\n\n"
        finally:
            # B-39 parity (CONTRACT §10): count mid-stream faults — the 200
            # response was already committed, so per-status accounting never
            # sees these; the dashboard must not report false health.
            if upstream_error is not None or mid_fault or getattr(state, "upstream_error", None):
                try:
                    metrics.record(error=True)
                except Exception:
                    pass
            try:
                upstream_resp.release()
            except Exception:
                pass
            await KEY_POOL.release(key_entry)

# Advanced streaming state machines
class AnthropicStreamState:
    def __init__(self, model):
        self.model = model
        self.index = -1  # first content block must be index 0 (Anthropic SDK)
        self.message_started = False
        self.current_block = None
        self.tool_map = {}
        # R-03/B-39 parity with common.translations.anthropic_stream: set when
        # an upstream error frame (or injected premature-close error) fired, so
        # the stream driver can count the fault in metrics (CONTRACT §10).
        self.upstream_error = None
        # R-02: Anthropic indices of tool_use blocks still open. Parallel tool
        # calls must remain open CONCURRENTLY (see common/translations).
        self.open_tool_blocks = set()
        self.finished = False
        # R9: unique per stream (ms alone collides across concurrent turns).
        self.msg_id = f"msg-{int(time.time()*1000)}-{secrets.token_hex(4)}"
        # P0-4: stateful special-token scrubbers (one per channel) — catch
        # tokens fragmented across chunks ('<un' + 'k>').
        self._tok_text = _SpecialTokenFilter()
        self._tok_reason = _SpecialTokenFilter()
        # R5 audit: stateful MiniMax DSML markup suppressor (cross-chunk).
        # Complete markup is re-emitted as real tool_use blocks in done() —
        # parity with common/translations/anthropic_stream.py (CONTRACT §7)
        # and with the non-streaming openai_to_anthropic translator.
        self._dsml_text = _DsmlMarkupFilter()

    def _close_nontool_block(self, events):
        """Close the open text/thinking block, flushing any text withheld by
        the special-token filter into its own channel first (P0-4). Tool
        blocks are tracked separately (R-02) and are NOT closed here."""
        if self.current_block is None or self.current_block == "tool_use":
            return
        if self.current_block == "thinking":
            rest = self._tok_reason.flush()
            if rest:
                events.append({"type": "content_block_delta", "data": {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "thinking_delta", "thinking": rest}}})
        elif self.current_block == "text":
            rest = self._tok_text.flush()
            if rest:
                events.append({"type": "content_block_delta", "data": {
                    "type": "content_block_delta", "index": self.index,
                    "delta": {"type": "text_delta", "text": rest}}})
        events.append({"type": "content_block_stop", "data": {
            "type": "content_block_stop", "index": self.index}})
        self.current_block = None

    def _close_all_tool_blocks(self, events):
        for _ti in sorted(self.open_tool_blocks):
            events.append({"type": "content_block_stop", "data": {"type": "content_block_stop", "index": _ti}})
        self.open_tool_blocks.clear()
        if self.current_block == "tool_use":
            self.current_block = None

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

        # R-03: surface a mid-stream upstream error frame instead of silently
        # dropping it and closing with a fabricated end_turn.
        if isinstance(chunk, dict) and chunk.get("error") is not None and "choices" not in chunk:
            err = chunk["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            etype = (err.get("type") if isinstance(err, dict) else None) or "api_error"
            logger.error(f"[nous] upstream error frame mid-stream: {msg}")
            self.upstream_error = str(msg or 'upstream error')  # R-03/B-39
            self._close_all_tool_blocks(events)
            self._close_nontool_block(events)
            events.append({"type": "error", "data": {
                "type": "error",
                "error": {"type": etype, "message": str(msg)[:2000]}}})
            # stop_reason=None (audit 2026-08-03): the turn FAILED — claiming
            # end_turn fabricates a clean completion. `error` is the real
            # signal; message_stop still follows so no client hangs.
            events.append({"type": "message_delta", "data": {
                "type": "message_delta",
                "delta": {"stop_reason": None, "stop_sequence": None},
                "usage": self._usage()}})
            events.append({"type": "message_stop", "data": {"type": "message_stop"}})
            self.finished = True
            return events

        if "choices" not in chunk:
            return events
        ch = (chunk.get("choices") or [{}])[0]
        delta = ch.get("delta", {}) or {}

        # reasoning / thinking delta
        reason = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reason, str) and reason:
            # P0-4: scrub tokenizer specials (incl. cross-chunk fragments).
            reason = self._tok_reason.feed(reason)
        else:
            reason = None
        if reason:
            if self.current_block != "thinking":
                # P3-4 fix: close ONLY the current text/thinking block.
                # Tool blocks stay OPEN (R-02/shared parity): a reasoning blip
                # between tool-argument fragments must not orphan them.
                self._close_nontool_block(events)
                self.index += 1
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.index,
                    # P1-2: thinking blocks require a `signature` field.
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                }})
                self.current_block = "thinking"
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "thinking_delta", "thinking": reason},
            }})

        # R5 audit: suppress DSML markup statefully (cross-chunk safe) — the
        # old per-chunk `'DSML' in chunk` check leaked parameter values and
        # closing tags of fragmented markup, and dropped whole chunks of
        # legitimate text that merely mentioned DSML. Complete markup is
        # recovered as tool_use blocks in done().
        content = delta.get("content")
        if isinstance(content, str) and content:
            content = self._dsml_text.feed(content)
        else:
            content = None
        if isinstance(content, str) and content:
            # P0-4: scrub tokenizer specials (incl. cross-chunk fragments).
            content = self._tok_text.feed(content)
        else:
            content = None

        if content:
            if self.current_block != "text":
                # P3-4 fix: close ONLY the current text/thinking block (see
                # reasoning branch above); tool blocks stay open.
                self._close_nontool_block(events)
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
                # R-02: close only a text/thinking block here. Closing the
                # previous TOOL block orphaned its later argument fragments
                # (content_block_delta on a closed index -> Claude Code drops
                # the tool call and the agent turn stalls). P0-4: flush any
                # filter-withheld text into its channel before the close.
                self._close_nontool_block(events)
                self.index += 1
                self.tool_map[idx] = self.index
                self.open_tool_blocks.add(self.index)
                tid = tc.get("id") or f"toolu_{self.index}-{secrets.token_hex(3)}"
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
            # R-02: close all open tool blocks, then any text/thinking block
            # (the close helpers flush filter-withheld text first, P0-4).
            self._close_all_tool_blocks(events)
            self._close_nontool_block(events)
            # R5 audit: drain DSML-withheld text + re-emit recovered DSML
            # tool_use blocks BEFORE the terminal frames (finish_reason stays
            # authoritative for stop_reason below).
            dsml_tool_n = self._drain_dsml_terminal(events)
            fr = ch.get("finish_reason")
            # B-06 fix: map STRICTLY from finish_reason. Forcing tool_use
            # whenever any tool had been seen made Claude Code wait for a
            # tool_result that would never be requested, and masked genuine
            # max_tokens truncation as tool_use.
            stop = {
                "stop": "end_turn", "length": "max_tokens",
                "tool_calls": "tool_use", "function_call": "tool_use",
                "content_filter": "refusal",
            }.get(fr, "end_turn")
            # R5 audit: DSML markup is the ONLY tool-call signal MiniMax
            # emits — the turn reports finish 'stop'. With recovered tool_use
            # blocks emitted above, end_turn would make the agent close the
            # turn and never execute the tool (done() + non-stream parity,
            # CONTRACT §8).
            if dsml_tool_n and stop == "end_turn":
                stop = "tool_use"
            events.append({"type": "message_delta", "data": {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": self._usage(chunk.get("usage") or {}),
            }})
            events.append({"type": "message_stop", "data": {"type": "message_stop"}})
            self.current_block = None
        return events

    def _drain_dsml_terminal(self, events):
        """R5 audit: at stream end, flush DSML-withheld clean text into the
        text channel and re-emit complete DSML tool markup collected
        mid-stream as real tool_use blocks (parity with the non-streaming
        openai_to_anthropic translator + common/translations/anthropic_stream
        — CONTRACT §7). Returns the number of recovered tool calls."""
        pre = self._dsml_text.flush()
        if pre:
            self._tok_text.feed(pre)
        rest = self._tok_text.flush()
        if rest:
            if self.current_block != "text" or self.open_tool_blocks:
                self._close_all_tool_blocks(events)
                self._close_nontool_block(events)
                self.index += 1
                events.append({"type": "content_block_start", "data": {
                    "type": "content_block_start", "index": self.index,
                    "content_block": {"type": "text", "text": ""}}})
                self.current_block = "text"
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "text_delta", "text": rest}}})
        tools = []
        markup = getattr(self._dsml_text, "collected_text", "") or ""
        if markup:
            try:
                _clean, tools = _parse_dsml_from_text(markup)
            except Exception:
                tools = []
        for tu in tools:
            if not isinstance(tu, dict):
                continue
            self._close_all_tool_blocks(events)
            self._close_nontool_block(events)
            self.index += 1
            try:
                args_json = json.dumps(tu.get("input") or {}, ensure_ascii=False)
            except Exception:
                args_json = "{}"
            events.append({"type": "content_block_start", "data": {
                "type": "content_block_start", "index": self.index,
                "content_block": {"type": "tool_use",
                                  "id": tu.get("id") or f"toolu_dsml_{self.index}-{secrets.token_hex(3)}",
                                  "name": tu.get("name") or "", "input": {}}}})
            events.append({"type": "content_block_delta", "data": {
                "type": "content_block_delta", "index": self.index,
                "delta": {"type": "input_json_delta", "partial_json": args_json}}})
            self.open_tool_blocks.add(self.index)
            self.current_block = "tool_use"
            self.tool_map[f"dsml_{self.index}"] = self.index
        # Close the recovered tool blocks before the terminal frames.
        self._close_all_tool_blocks(events)
        return len([t for t in tools if isinstance(t, dict)])

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
        # B-06: capture before clearing.
        was_in_tool_block = (self.current_block == "tool_use") or bool(self.open_tool_blocks)
        # R-02: close every open tool block first, then text/thinking (P0-4:
        # the close helpers flush any filter-withheld text into its channel).
        self._close_all_tool_blocks(events)
        self._close_nontool_block(events)
        # R5 audit: drain DSML-withheld clean text + re-emit recovered DSML
        # tool markup as real tool_use blocks (stream/non-stream parity).
        dsml_tool_n = self._drain_dsml_terminal(events)
        # B-06: this is the no-finish_reason path, so inferring from tool state
        # is legitimate — but narrow it to a tool block that was still open.
        stop = "tool_use" if (self.tool_map and was_in_tool_block) or dsml_tool_n else "end_turn"
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
        self.rsn_index = None
        self.rsn_id = f"rsn-{int(time.time()*1000)}"
        self.acc_reason = ""
        self.started = False
        self._active_tool_id = None
        self._completed = False
        self._finished = False
        self.accum_usage = {}
        self.upstream_error = None  # R-03
        # P0-4: stateful special-token scrubbers (cross-chunk fragments).
        # final_text/acc_reason accumulate the FILTERED text only; withheld
        # remainders are flushed in done() before the *.done events.
        self._tok_text = _SpecialTokenFilter()
        self._tok_reason = _SpecialTokenFilter()
        # R5 audit: stateful DSML markup suppressor on the visible text
        # channel (cross-chunk); complete markup becomes function_call items.
        self._dsml_text = _DsmlMarkupFilter()

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
        # CODEX-RESP-02: response.created MUST carry a FULL response object —
        # the openai SDK (Codex) builds its stream snapshot from it and
        # appends output_item.added events to `response.output`; with the
        # minimal {id, model, status} the snapshot's `output` is None and the
        # first output_item.added crashes the parser
        # (AttributeError: 'NoneType' object has no attribute 'append').
        return [
            self.emit("response.created", {"response": {
                "id": rid, "object": "response", "created_at": int(time.time()),
                "model": self.model, "status": "in_progress", "output": [],
                # P1-3: full usage shape (details structures) for strict SDKs.
                "usage": _responses_usage(),
            }}),
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
        # R5 audit: suppress DSML markup first (cross-chunk), then P0-4 scrub
        # special tokens. Only clean, emittable text is accumulated/streamed;
        # the withheld tail is flushed in done().
        text = self._tok_text.feed(self._dsml_text.feed(text))
        if not text:
            return ""
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
        # CODEX-RESP-02: the OpenAI Responses API streams tool arguments as
        # `response.function_call_arguments.delta` (NOT response.function_call.delta).
        # The openai SDK (Codex) rejects the wrong name, so tool arguments were
        # never accumulated and tool-calling turns hung/broke.
        events.append(self.emit("response.function_call_arguments.delta", {
            "item_id": call_id, "output_index": self.tool_acc[call_id]["output_index"], "delta": args,
        }))
        return events

    def _normalize_usage(self, u):
        if not u:
            u = self.accum_usage or {}
        else:
            self.accum_usage.update(u)
        _in, _out, _cached, _rsn = _tokens_from_chat_usage(u)
        # P1-3 fix: the Responses usage object requires total_tokens alongside
        # input/output AND the *_details structures (strict SDK validation).
        return _responses_usage(_in, _out, _cached, _rsn)

    def done(self, usage=None):
        # MUST return a list — stream_with_heartbeat iterates this.
        # Idempotent: emit response.completed exactly once.
        if self._completed:
            return []
        self._completed = True
        norm = self._normalize_usage(usage)
        rid = self.rid
        # R-03: the upstream reported a mid-stream failure. Emit
        # response.failed instead of a fabricated response.completed, so the
        # client can detect the error and retry rather than persisting a
        # truncated answer as a successful turn.
        if getattr(self, "upstream_error", None):
            return [self.emit("response.failed", {"response": {
                "id": rid, "object": "response", "model": self.model,
                "status": "failed",
                "error": {"code": "upstream_error",
                          "message": str(self.upstream_error)[:2000]},
            }})]
        text = getattr(self, "final_text", "")
        events = []
        # P0-4/R5: release any text withheld by the DSML + special-token
        # filters before the terminal *.done events so the visible answer is
        # complete (DSML remnant still passes the token scrubber).
        _rest_text = self._tok_text.feed(self._dsml_text.flush()) + self._tok_text.flush()
        if _rest_text:
            text += _rest_text
            self.final_text = text
            events.append(self.emit("response.output_text.delta", {
                "item_id": "msg-1", "output_index": 0, "content_index": 0, "delta": _rest_text}))
        # P1-3: output_text parts carry `annotations` for strict SDK parsing.
        events.extend([
            self.emit("response.output_text.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "text": text}),
            self.emit("response.content_part.done", {"item_id": "msg-1", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []}}),
            self.emit("response.output_item.done", {"output_index": 0, "item": {"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]}}),
        ])
        # Close the reasoning item opened during thinking (if any).
        # CODEX-RESP-02: the reasoning item's `summary` MUST be a list (the SDK
        # model expects list[Summary]); an empty string triggers serializer
        # failures in the openai SDK.
        if self.reasoning_started:
            # P0-4: flush any withheld reasoning tail before the done event.
            _rest_rsn = self._tok_reason.flush()
            if _rest_rsn:
                self.acc_reason += _rest_rsn
                events.append(self.emit("response.reasoning_text.delta", {
                    "item_id": self.rsn_id, "output_index": self.rsn_index, "content_index": 0, "delta": _rest_rsn}))
            events.append(self.emit("response.reasoning_text.done", {
                "item_id": self.rsn_id, "output_index": self.rsn_index, "content_index": 0, "text": self.acc_reason,
            }))
            events.append(self.emit("response.output_item.done", {
                "output_index": self.rsn_index,
                "item": {"id": self.rsn_id, "type": "reasoning", "status": "completed",
                         "summary": [], "content": [{"type": "reasoning_text", "text": self.acc_reason}]},
            }))
        # R5 note: DSML markup collected on this surface is suppressed (not
        # re-emitted) for cross-wrapper parity (CONTRACT §8): tool-call
        # recovery happens on the Messages surface (stream + non-stream).
        _ = getattr(self._dsml_text, "collected_text", "")
        # Close every tool item that was opened (Codex hangs if a function_call
        # item is added but never marked done). CODEX-RESP-02: emit the
        # standard `response.function_call_arguments.done` before closing the
        # item so the SDK finalizes the parsed arguments.
        for call_id, info in self.tool_acc.items():
            events.append(self.emit("response.function_call_arguments.done", {
                "item_id": call_id, "output_index": info.get("output_index", 1),
                "name": info.get("name", ""), "arguments": info.get("args", ""),
            }))
            events.append(self.emit("response.output_item.done", {
                "output_index": info.get("output_index", 1),
                "item": {
                    "id": call_id, "type": "function_call", "status": "completed",
                    "call_id": call_id, "name": info.get("name", ""),
                    "arguments": info.get("args", ""),
                },
            }))
        # Build the final output array sorted by output_index (0=text, 1=reasoning,
        # 2+ = tools) so the client's response.completed parse is well-ordered.
        outputs_by_index = {
            0: {"id": "msg-1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]},
        }
        if self.reasoning_started:
            outputs_by_index[self.rsn_index] = {"id": self.rsn_id, "type": "reasoning", "status": "completed",
                                                "summary": [], "content": [{"type": "reasoning_text", "text": self.acc_reason}]}
        for call_id, info in self.tool_acc.items():
            outputs_by_index[info.get("output_index", 1)] = {"id": call_id, "type": "function_call", "status": "completed", "call_id": call_id, "name": info.get("name", ""), "arguments": info.get("args", "")}
        output = [outputs_by_index[i] for i in sorted(outputs_by_index)]
        events.append(self.emit("response.completed", {"response": {
            "id": rid, "object": "response", "created_at": int(time.time()),
            "model": self.model, "status": "completed", "output": output, "usage": norm,
        }}))
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

        # R-03: an upstream {"error": ...} frame has no "choices" and was
        # silently dropped, so the turn ended with a fabricated
        # response.completed. Record it so done() emits response.failed.
        if isinstance(chunk, dict) and chunk.get("error") is not None and "choices" not in chunk:
            _e = chunk["error"]
            self.upstream_error = (_e.get("message") if isinstance(_e, dict) else str(_e)) or "upstream error"
            logger.error(f"[nous responses] upstream error frame: {self.upstream_error}")
            return events

        # R-08: a frame may legally carry an EMPTY choices array (usage-only
        # frames, some provider keep-alives). `chunk["choices"][0]` raised
        # IndexError -> HTTP 500 mid-stream, killing the turn.
        _choices = chunk.get("choices") or []
        if not _choices:
            return events

        ch = _choices[0] or {}
        delta = ch.get("delta", {}) or {}

        # Text
        # F2: `content` is only str for the OpenAI-compatible shape; some
        # upstreams stream multi-part content arrays. Guard the type so a
        # list doesn't raise TypeError mid-stream (HTTP 500 kills the turn),
        # mirroring nvidia-python responses_compat:603/674.
        content = delta.get("content")
        if isinstance(content, str):
            if content:
                ev = self.delta(content)
                if ev:
                    events.append(ev)
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
            if parts:
                ev = self.delta("".join(parts))
                if ev:
                    events.append(ev)

        # Reasoning (Nous reasoning_content / reasoning) — MUST be streamed so the
        # client keeps receiving progress during the model's thinking phase.
        # The previous behavior DROPPED reasoning, leaving a multi-second silent
        # gap on the SSE stream that tripped client-side idle timeouts (Codex /
        # OpenAI SDK "stops mid-way"). Mirror reference nvidia-python
        # responses_compat: open a 'reasoning' output item then stream deltas.
        reason_delta = (
            delta.get("reasoning_content") if isinstance(delta.get("reasoning_content"), str)
            else (delta.get("reasoning") if isinstance(delta.get("reasoning"), str) else "")
        )
        if reason_delta:
            # P0-4: scrub special tokens from the reasoning channel too.
            reason_delta = self._tok_reason.feed(reason_delta)
        if reason_delta:
            if not self.reasoning_started:
                self.reasoning_started = True
                self.rsn_index = self._next_tool_index
                self._next_tool_index += 1
                events.append(self.emit("response.output_item.added", {
                    "output_index": self.rsn_index,
                    "item": {"id": self.rsn_id, "type": "reasoning", "status": "in_progress", "summary": [], "content": []},
                }))
            self.acc_reason += reason_delta
            events.append(self.emit("response.reasoning_text.delta", {
                "item_id": self.rsn_id, "output_index": self.rsn_index, "content_index": 0, "delta": reason_delta,
            }))

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
    """B-39 fix: nous was the only wrapper whose metrics had NO persistence —
    every counter reset to zero on each restart, so the dashboard's
    total_requests/error_rate were meaningless after a deploy. Now persists to
    a JSON snapshot periodically and reloads it at startup (blackbox /
    opencode / openrouter parity)."""

    def __init__(self):
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self.start = time.time()
        self._lock = threading.Lock()
        self._persist_interval = float(os.environ.get('METRICS_PERSIST_SEC', '60'))
        self._last_persist = time.time()
        self._load_persisted()

    def _persist_path(self) -> str:
        return str(Path(__file__).resolve().parents[1] / 'metrics-snapshot.json')

    def _load_persisted(self):
        try:
            path = self._persist_path()
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                self.requests = int(data.get('requests', 0))
                self.tokens_in = int(data.get('tokens_in', 0))
                self.tokens_out = int(data.get('tokens_out', 0))
                self.errors = int(data.get('errors', 0))
        except Exception:
            pass

    def _persist(self):
        try:
            with open(self._persist_path(), 'w') as f:
                json.dump({
                    'requests': self.requests,
                    'tokens_in': self.tokens_in,
                    'tokens_out': self.tokens_out,
                    'errors': self.errors,
                    'saved_at': time.time(),
                }, f)
        except Exception:
            pass

    def record(self, prompt=0, completion=0, error=False):
        with self._lock:
            self.requests += 1
            self.tokens_in += prompt
            self.tokens_out += completion
            if error:
                self.errors += 1
            now = time.time()
            if now - self._last_persist >= self._persist_interval:
                self._last_persist = now
                self._persist()

    def snapshot(self):
        uptime = time.time() - self.start
        return {
            "uptime_seconds": int(uptime),
            "total_requests": self.requests,
            "total_tokens": self.tokens_in + self.tokens_out,
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            # B-39 visibility (audit 2026-08-03 round-4): the counter itself was
            # never exposed — only the derived rate. Dashboards and /metrics/prom
            # had no way to show the absolute error count.
            "total_errors": self.errors,
            "error_rate": round(self.errors / max(1, self.requests), 4)
        }

metrics = Metrics()


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
import hmac
rate_limits = defaultdict(list)
_rate_limit_lock = threading.Lock()

def check_rate_limit(ip: str):
    """Return True if request is allowed, False if rate-limited.
    RATE_LIMIT_RPM=0 disables per-IP limiting entirely."""
    if RATE_LIMIT_RPM <= 0:
        return True
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
            await KEY_POOL.heal_in_flight()
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
    logger.info(f"[wrapper-nous] Starting graceful shutdown...")
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
    # B-22: `global _SESSION` removed — never assigned in this scope.
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
    # WRAPPER_CONTRACT §10: every response carries X-Request-ID and
    # X-Process-Time. The request id was logged but never returned, breaking
    # distributed tracing for clients that correlate by header.
    response.headers["X-Request-ID"] = request_id
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


# R-01 fix: reject non-object JSON bodies with a shaped 400 instead of letting
# `body.get(...)` raise AttributeError -> HTTP 500 (see common/body_guard.py).
try:
    from common.body_guard import JSONBodyGuard as _JSONBodyGuard
    app.add_middleware(_JSONBodyGuard)
except ImportError:  # pragma: no cover
    pass

if _HAS_SIZE_LIMITER:
    app.add_middleware(RequestSizeLimiter)

async def _auth_check(request: Request):
    """B-28/B-29/B-30 fix: fail-closed, rotation-aware, byte-safe auth.

    Three defects fixed here:
      B-28 — `if not BEARER_TOKEN: return` allowed ALL requests when the token
             was unset (open relay on a truncated/failed .env reload).
      B-29 — comparing against the module-level BEARER_TOKEN captured at import
             meant rotation (and revocation) required a full restart.
      B-30 — hmac.compare_digest on two `str` raises TypeError on non-ASCII
             input, surfacing as an unhandled 500 instead of a clean 401.
    """
    if request.method == 'OPTIONS':
        return  # CORS preflight passes without auth
    if _HAS_SHARED_AUTH:
        res = _shared_check_auth(request.headers, surface=request.url.path)
        if not res.ok:
            raise HTTPException(res.status, detail={"error": {
                "type": "authentication_error", "message": res.message}})
        return
    if os.environ.get('DISABLE_AUTH'):
        return
    # B-29: re-read from the environment so .env rotation takes effect.
    token_cfg = (os.environ.get('BEARER_TOKEN') or '').strip()
    if not token_cfg:
        raise HTTPException(503, detail={"error": {
            "type": "authentication_error", "message": "Server auth not configured"}})
    auth = request.headers.get("authorization", "") or request.headers.get("x-api-key", "")
    token = auth.replace("Bearer ", "", 1).strip()
    if not token or not hmac.compare_digest(
            token.encode('utf-8'), token_cfg.encode('utf-8')):
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
    entry = await KEY_POOL.peek()
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


@app.get("/api/tags")
async def api_tags():
    """Ollama-compatible model discovery — PUBLIC (no auth).

    Returns the model list in Ollama's /api/tags format so Ollama clients
    can discover models served by this wrapper.
    """
    try:
        models_data = await models()
    except Exception:
        models_data = {"data": []}
    out_models = []
    for m in (models_data.get("data") or []):
        if not isinstance(m, dict):
            continue
        mid = m.get("id", "")
        if not mid:
            continue
        family = mid.split("/")[0] if "/" in mid else mid
        out_models.append({
            "name": mid, "model": mid,
            "modified_at": "1970-01-01T00:00:00Z", "size": 0, "digest": "",
            "details": {
                "parent_model": "", "format": "gguf",
                "family": family, "families": [family],
                "parameter_size": "", "quantization_level": "",
            },
        })
    return {"models": out_models}


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
    # B-23 fix: the previous `request_id = ...` / `start_time = ...` locals here
    # were computed and then never used — dead code implying per-request
    # observability that did not exist. Correlation ID and latency are set
    # centrally by the HTTP middleware (X-Request-ID / X-Process-Time), so the
    # duplicated locals are removed rather than reimplemented here.
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
    # TRANSPARENT PROXY: do NOT drop client params (was silently stripping
    # n, logprobs, logit_bias, user, frequency_penalty, presence_penalty).
    # Forward the body verbatim — upstream will reject unsupported params
    # with a clear 400 if it doesn't accept them.
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
    extra_h = _build_forward_headers(request.headers)  # transparent: forward all client headers

    # COMPATIBILITY_LAYER=2: Anthropic upstream — translate the OpenAI chat
    # request to Anthropic Messages and translate the response back.
    if await _upstream_is_anthropic():
        anthro_body = openai_chat_to_anthropic_request(body)
        anthro_body["stream"] = is_stream
        status, result, key_entry = await post_nous_with_retries(
            anthro_body, stream=is_stream, extra_headers=extra_h, path="v1/messages")
        _fire_and_forget(record_model_result(body.get("model", ""), key_entry, status, result, "/v1/chat/completions"), "model-result")
        metrics.record(error=(status != 200))
        if status != 200:
            return JSONResponse(status_code=status, content=result)
        if is_stream:
            async def gen():
                try:
                    async for frame in _translate_anthropic_stream_to_openai_chat(result, body.get("model", ""), HEARTBEAT_MS / 1000.0):
                        yield frame
                finally:
                    await KEY_POOL.release(key_entry)
            return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        return JSONResponse(anthropic_to_openai_response(result, body.get("model", "")))

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
        if result.get("type") == "message" and "content" in result:
            result = anthropic_to_openai_response(result, body.get("model", ""))
        # P0-4: scrub special tokens from the non-stream reply body too.
        _scrub_openai_response_inplace(result)
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
    extra_h = _build_forward_headers(request.headers)  # transparent: forward all client headers

    # COMPATIBILITY_LAYER=2: Anthropic upstream — Responses → Chat → Anthropic
    # request; translate the Anthropic response back through OpenAI Chat to
    # Responses.
    if await _upstream_is_anthropic():
        anthro_body = openai_chat_to_anthropic_request(chat_body)
        anthro_body["stream"] = is_stream
        status, result, key_entry = await post_nous_with_retries(
            anthro_body, stream=is_stream, extra_headers=extra_h,
            client_surface="openai_responses", path="v1/messages")
        _fire_and_forget(record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/responses"), "model-result")
        metrics.record(error=(status != 200))
        if status != 200:
            return JSONResponse(status_code=status, content=result)
        if is_stream:
            async def gen():
                try:
                    async for frame in _translate_anthropic_stream_to_openai_chat(result, chat_body.get("model", ""), HEARTBEAT_MS / 1000.0):
                        yield frame
                finally:
                    await KEY_POOL.release(key_entry)
            return StreamingResponse(
                _translate_openai_chat_sse_to_responses(gen(), chat_body.get("model", "")),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        oai_chat = anthropic_to_openai_response(result, chat_body.get("model", ""))
        resp = chat_to_responses(chat_body.get("model", ""), oai_chat)
        _amsg = (oai_chat.get("choices") or [{}])[0].get("message", {})
        await store_conversation(principal, resp["id"], list(chat_body.get("messages", [])) + [
            {"role": "assistant", "content": _amsg.get("content"),
             "tool_calls": _amsg.get("tool_calls") or None}])
        return resp

    status, result, key_entry = await post_nous_with_retries(chat_body, stream=is_stream, extra_headers=extra_h, client_surface="openai_responses")
    # F3 round-2 fix: fire-and-forget with retained reference (_BG_TASKS pattern).
    _fire_and_forget(record_model_result(chat_body.get("model", ""), key_entry, status, result, "/v1/responses"), "model-result")
    # N-20 fix: record metrics on this surface too (was only chat_completions).
    metrics.record(error=(status != 200))
    if status != 200:
        return JSONResponse(status_code=status, content=result)

    if is_stream:
        rid = _new_response_id()  # R7: unique per turn (history-store safety)
        state = ResponsesStreamState(rid, chat_body["model"])
        async def gen():
            # Codex requires output_item.added BEFORE first delta.
            for ev in state.start():
                yield ev
            try:
                # B-11 fix: the old serializer was `str(x)` for non-str events,
                # which writes a Python repr ({'type': ...} with single quotes)
                # into the SSE body — invalid JSON that Codex either ignores or
                # renders as text. Emit a proper event:/data: frame instead.
                async for line in stream_with_heartbeat(result, _responses_sse_serialize, state=state, key_entry=key_entry):
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

    # P0-4: scrub before conversion AND before the history is persisted, so
    # special tokens cannot poison later previous_response_id turns.
    _scrub_openai_response_inplace(result)
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
    # WRAPPER_CONTRACT §4: unknown roles / orphan tool messages are rejected.
    for _m in body.get('messages', []) or []:
        if isinstance(_m, dict) and _m.get('role') not in (None, 'user', 'assistant', 'tool', 'system', 'developer'):
            return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': f"Invalid role: {_m.get('role')!r}"}})
        if isinstance(_m, dict) and _m.get('role') == 'tool' and not _m.get('tool_use_id') and not _m.get('tool_call_id'):
            return JSONResponse(status_code=400, content={'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'tool message requires tool_use_id'}})
    requested = body.get("model")
    if free_only_enabled() and requested:
        resolved = resolve_model(requested)
        if not model_allowed(requested) and not model_allowed(resolved):
            return JSONResponse(status_code=400, content=free_only_anthropic_error(requested))
    # COMPATIBILITY_LAYER=2: Anthropic upstream — the /v1/messages surface
    # passes through verbatim (model alias already applied above).
    if await _upstream_is_anthropic():
        if free_only_enabled() and body.get("model") and not model_allowed(body.get("model", "")):
            return JSONResponse(status_code=400, content=free_only_anthropic_error(requested or body.get("model") or ""))
        is_stream = body.get("stream", False)
        extra_h = _build_forward_headers(request.headers)
        status, result, key_entry = await post_nous_with_retries(
            body, stream=is_stream, extra_headers=extra_h, client_surface="anthropic_messages", path="v1/messages")
        _fire_and_forget(record_model_result(body.get("model", ""), key_entry, status, result, "/v1/messages"), "model-result")
        metrics.record(error=(status != 200))
        if status != 200:
            return JSONResponse(status_code=status, content=result)
        if is_stream:
            async def gen():
                try:
                    async for frame in _passthrough_anthropic_sse(result, HEARTBEAT_MS / 1000.0):
                        yield frame
                finally:
                    await KEY_POOL.release(key_entry)
            return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        return JSONResponse(result)

    chat_body = anthropic_to_openai(body)
    # Note: anthropic_to_openai may map thinking→REASONING_MODEL (pre-existing);
    # FREE_ONLY still enforces the *outgoing* model is free when enabled.
    if free_only_enabled() and chat_body.get("model") and not model_allowed(chat_body.get("model", "")):
        return JSONResponse(status_code=400, content=free_only_anthropic_error(chat_body.get("model") or requested or ""))
    is_stream = body.get("stream", False)
    extra_h = _build_forward_headers(request.headers)  # transparent: forward all client headers

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
# Per project principle: dashboard must be fast/precise/accessible without
# token. Metrics endpoints are PUBLIC so the dashboard can render live data
# without auth friction. Security is NOT mandatory for telemetry.
@app.get("/metrics")
async def get_metrics(request: Request):
    # R12 (openrouter R7 parity, CONTRACT §10): include the live per-key pool
    # stats so in-flight reservations are visible on the JSON surface too.
    snap = metrics.snapshot()
    snap['pool'] = KEY_POOL.all_stats()
    snap['in_flight'] = sum(k.in_flight for k in KEY_POOL.keys)
    return snap

@app.get("/metrics/prom")
async def prom(request: Request):
    snap = metrics.snapshot()
    lines = [
        f'# HELP wrapper_nous_requests_total Total requests\nwrapper_nous_requests_total {snap["total_requests"]}',
        f'wrapper_nous_tokens_total {snap["total_tokens"]}',
        f'# HELP wrapper_nous_errors_total Total errors (incl. mid-stream faults)\nwrapper_nous_errors_total {snap["total_errors"]}',
    ]
    # R14 (CONTRACT §10 parity): include pool-level Prometheus series like the
    # four sibling wrappers (key availability, in-flight, per-key rpm/blocked/
    # failures) instead of only the 3 hardcoded counters above.
    return Response("\n".join(lines) + "\n" + KEY_POOL.prom_metrics(), media_type="text/plain")

@app.get("/metrics/model-status")
async def model_status(request: Request):
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

# ── Catalog + MCP Integration ──────────────────────────────────────────
try:
    from common.catalog_integration import setup_catalog_routes, setup_mcp_server, free_only_enabled as _cfe
    setup_catalog_routes(app)
    setup_mcp_server(app, "nous")
    # Override free_only with shared version
    free_only_enabled = _cfe
    _HAS_CATALOG_INTEGRATION = True
except ImportError as _cie:
    _HAS_CATALOG_INTEGRATION = False
    pass

# catch-all


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """Embeddings endpoint — not supported by this upstream provider.

    Returns a clear 501 so SDK clients get a structured error instead of
    a 404 catch-all. Operators who need embeddings should use nvidia-python
    or openrouter wrappers which DO support embeddings.
    """
    # B-31 fix: this endpoint previously parsed an arbitrary JSON body with NO
    # auth and NO rate limit — unauthenticated CPU/memory work reachable by
    # anyone who can hit the port. Gate it like every other POST surface.
    await _auth_check(request)
    if not check_rate_limit(request.client.host if request.client else "unknown"):
        return JSONResponse(status_code=429, content={"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    try:
        # B-19: validate the body is well-formed JSON (so clients get 400 not
        # 501 for malformed input) without binding an unused variable.
        await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
                            status_code=400)
    return JSONResponse({
        "error": {
            "message": "Embeddings not supported by this provider. Use nvidia-python (port 9101) or openrouter (port 9106) for embeddings.",
            "type": "not_implemented_error",
            "code": 501,
        }
    }, status_code=501)

@app.api_route("/{path:path}", methods=["GET", "POST"])
async def catch_all(path: str, request: Request):
    # B-31 parity: unknown POST surfaces are still POST surfaces — authenticate
    # and rate-limit before doing any work. GET 404 discovery remains cheap.
    if request.method == "POST":
        await _auth_check(request)
        if not check_rate_limit(request.client.host if request.client else "unknown"):
            return JSONResponse(status_code=429, content={"error": {"type": "rate_limit_error", "message": "Too many requests"}})
    return JSONResponse(status_code=404, content={"error": {"message": f"Unsupported: {path}", "type": "not_found_error"}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
