#!/usr/bin/env python3
"""
key_pool.py — resilient multi-key pool for OpenCode Zen.

Goals:
- one failing/limited key must not fail the wrapper;
- rotate across all configured keys by effective load;
- temporarily cool down keys that return 429/auth/quota/5xx failures;
- keep in-flight accounting exact for long-running agent streams.
"""

import os
import time
import asyncio
import logging
from typing import Optional, List

logger = logging.getLogger('wrapper-opencode')


class KeyEntry:
    def __init__(self, label: str, api_key: str):
        self.label = label
        self.api_key = api_key
        # Honor provider-prefixed env vars first (OPENCODE_HARD_LIMIT_RPM),
        # fall back to bare HARD_LIMIT_RPM for parity with siblings.
        self.soft_rpm: int = int(os.environ.get('OPENCODE_SOFT_LIMIT_RPM',
                                                 os.environ.get('SOFT_LIMIT_RPM', '30')))
        self.hard_rpm: int = int(os.environ.get('OPENCODE_HARD_LIMIT_RPM',
                                                 os.environ.get('HARD_LIMIT_RPM', '40')))
        self.timestamps: List[float] = []
        self.hard_blocked_until: float = 0.0
        self.block_reason: str = ''
        # NO MODEL FALLBACK: model-scoped blocks ensure error on model A
        # at key1 only blocks key1 for model A — model B can still use key1.
        self.model_blocks: dict[str, tuple[float, str]] = {}  # model_id → (blocked_until, reason)
        self.in_flight: int = 0
        self.total_requests: int = 0
        self.total_429s: int = 0
        self.total_failures: int = 0
        self.last_used: float = 0.0

    @property
    def effective_load(self) -> int:
        return self.current_rpm() + self.in_flight

    def increment_in_flight(self):
        self.in_flight += 1

    def decrement_in_flight(self):
        if self.in_flight > 0:
            self.in_flight -= 1

    def current_rpm(self, window: int = 60) -> int:
        now = time.time()
        cutoff = now - window
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        return len(self.timestamps)

    def seconds_until_below(self, limit: int, window: int = 60) -> float:
        now = time.time()
        ts = sorted([t for t in self.timestamps if now - t < window])
        if len(ts) < limit:
            return 0.0
        idx = max(0, len(ts) - limit)
        return max(0.0, window - (now - ts[idx]))

    def record(self):
        now = time.time()
        self.timestamps.append(now)
        self.total_requests += 1
        self.last_used = now

    def block(self, seconds: int, reason: str):
        seconds = max(1, min(int(seconds or 1), int(os.environ.get('KEY_COOLDOWN_MAX_SEC', '300'))))
        self.hard_blocked_until = max(self.hard_blocked_until, time.time() + seconds)
        self.block_reason = reason
        self.total_failures += 1
        if reason == 'rate_limit':
            self.total_429s += 1
        logger.warning(f'[opencode] key {self.label} cooled down for {seconds}s ({reason})')

    def block_model(self, model_id: str, seconds: int, reason: str):
        """NO MODEL FALLBACK: cool down only this key+model pair.

        Error on model A at key1 blocks key1 ONLY for model A.
        Model B can still use key1 because it's a different model.
        """
        if not model_id:
            return
        seconds = max(1, min(int(seconds or 1), int(os.environ.get('KEY_COOLDOWN_MAX_SEC', '300'))))
        self.model_blocks[model_id] = (time.time() + seconds, reason)
        self.total_failures += 1
        if reason == 'rate_limit':
            self.total_429s += 1
        logger.warning(f'[opencode] key {self.label} model {model_id!r} cooled down for {seconds}s ({reason})')

    def is_model_blocked(self, model_id: str) -> bool:
        """Side-effect-free model-block predicate (B-37 parity)."""
        if not model_id:
            return False
        entry = self.model_blocks.get(model_id)
        if not entry:
            return False
        blocked_until, _reason = entry
        return time.time() < blocked_until

    def expire_model_blocks(self) -> None:
        """Clear elapsed model-scoped blocks. Caller must hold the pool lock."""
        now = time.time()
        for model_id, (blocked_until, _reason) in list(self.model_blocks.items()):
            if blocked_until <= now:
                self.model_blocks.pop(model_id, None)

    def is_hard_blocked(self) -> bool:
        """B-37 fix: side-effect-free predicate.

        Previously this CLEARED hard_blocked_until as a side effect of being
        asked "are you blocked?". It is also called from stats()/health_json()
        outside the pool lock, so a concurrent metrics scrape could clear a
        live block. Expiry is now explicit (expire_block()), done under the
        lock in acquire().
        """
        return time.time() < self.hard_blocked_until

    def expire_block(self) -> None:
        """Clear an elapsed hard block. Caller must hold the pool lock."""
        if self.hard_blocked_until and time.time() >= self.hard_blocked_until:
            self.hard_blocked_until = 0.0
            self.block_reason = ''

    def stats(self, soft: int, hard: int) -> dict:
        rpm = self.current_rpm()
        now = time.time()
        return {
            'label': self.label,
            'current_rpm': rpm,
            'in_flight': self.in_flight,
            'effective_load': self.effective_load,
            'hard_blocked': self.is_hard_blocked(),
            'hard_blocked_remaining_s': max(0, round(self.hard_blocked_until - time.time(), 1)),
            'block_reason': self.block_reason or None,
            'model_blocks': {m: {'until': round(u - now, 1), 'reason': r} for m, (u, r) in self.model_blocks.items() if u > now},
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
            'total_failures': self.total_failures,
            'soft_rpm': self.soft_rpm or soft,
            'hard_rpm': self.hard_rpm or hard,
        }


class KeyPool:
    def __init__(self):
        self.keys: List[KeyEntry] = []
        self.soft_limit: int = 30
        self.hard_limit: int = 40
        self._lock = asyncio.Lock()
        self._in_flight_total = 0
        self._rr = 0

    def load_from_env(self):
        self.keys = []
        env_keys = []
        seen = set()
        for key_name, value in sorted(os.environ.items()):
            if not value or len(value.strip()) < 10:
                continue
            if key_name == 'OPENCODE_API_KEY' or key_name.startswith('OPENCODE_API_KEY_'):
                v = value.strip()
                if v in seen:
                    continue
                seen.add(v)
                env_keys.append(v)

        self.soft_limit = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_limit = int(os.environ.get('HARD_LIMIT_RPM', '40'))

        if env_keys:
            self.keys = [KeyEntry(f'key{i+1}', k) for i, k in enumerate(env_keys)]
        else:
            logger.warning('[opencode] No OPENCODE_API_KEY* found')

        logger.info(f'[opencode] Loaded {len(self.keys)} OpenCode key(s) soft={self.soft_limit} hard={self.hard_limit}')
        return self

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_hard_blocked() and k.current_rpm() < (k.hard_rpm or self.hard_limit))

    async def acquire(self, model: str = '') -> Optional[dict]:
        # OC-1: asyncio.Lock is cancellation-safe — a task cancelled while waiting
        # in the lock queue is removed on cancel, unlike the old hand-rolled
        # Mutex whose cancelled future permanently wedged the pool (whole-wrapper
        # hang under load).
        async with self._lock:
            # Default OFF for multi-agent localhost deployments; per-key RPM
            # limits already protect upstream. Bump cap to 500 for headroom.
            inflight_cap = int(os.environ.get('INFLIGHT_SOFT_CAP', '500'))
            load_shedding = os.environ.get('LOAD_SHEDDING_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')
            if load_shedding and sum(k.in_flight for k in self.keys) >= inflight_cap:
                logger.warning(f'[opencode] Load shedding: in-flight >= {inflight_cap}')
                return None

            # B-37: expire elapsed blocks explicitly, under the lock.
            for _k in self.keys:
                _k.expire_block()
                _k.expire_model_blocks()
            # NO MODEL FALLBACK: filter candidates by model-scoped blocks.
            # Error on model A at key1 blocks key1 ONLY for model A —
            # model B can still use key1 because it's a different model.
            candidates = [k for k in self.keys
                          if not k.is_hard_blocked()
                          and not (model and k.is_model_blocked(model))
                          and k.current_rpm() < (k.hard_rpm or self.hard_limit)]
            if not candidates:
                return None
            min_load = min(k.effective_load for k in candidates)
            best = [k for k in candidates if k.effective_load == min_load]
            # Stable round-robin tie-breaker so five keys are actually used.
            key = best[self._rr % len(best)]
            self._rr += 1
            key.record()
            key.increment_in_flight()
            self._in_flight_total += 1
            return {'key': key}

    def release(self, key: KeyEntry = None):
        if key is None:
            return
        key.decrement_in_flight()
        self._in_flight_total = max(0, self._in_flight_total - 1)

    def mark_failure(self, key: KeyEntry, status_code: int = 0, retry_after: int = None, reason: str = '', available_keys: int = None, model: str = ''):
        """NO MODEL FALLBACK: model-scoped block for 429/5xx.

        Error on model A at key1 blocks key1 ONLY for model A.
        Model B can still use key1 because it's a different model.
        Auth/quota errors (401/402/403) block the whole key (credential issue).
        """
        if key is None:
            return
        if status_code == 429:
            cooldown = retry_after or int(os.environ.get('RATE_LIMIT_COOLDOWN_SEC', '65'))
            # Model-scoped block: only block this key for this model.
            if model:
                key.block_model(model, cooldown, 'rate_limit')
            else:
                key.block(cooldown, 'rate_limit')
        elif status_code in (401, 403, 402):
            # Auth/quota = credential issue → block whole key.
            key.block(retry_after or int(os.environ.get('AUTH_KEY_COOLDOWN_SEC', '300')), 'auth_or_quota')
        elif status_code >= 500 or status_code in (408, 409):
            cooldown = retry_after or int(os.environ.get('TRANSIENT_KEY_COOLDOWN_SEC', '15'))
            # OC-13: with no other ready key, a single transient blip shouldn't
            # shed 100% of traffic for the full cooldown — back off briefly so a
            # retry (or the next request) can still use this key.
            if available_keys is not None and available_keys <= 0 and cooldown > 1:
                cooldown = 1
            # Model-scoped block for transient errors too.
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

    def all_stats(self) -> list:
        return [k.stats(self.soft_limit, self.hard_limit) for k in self.keys]

    def prom_metrics(self) -> str:
        lines = [
            '# HELP opencode_keys_total Total keys',
            '# TYPE opencode_keys_total gauge',
            f'opencode_keys_total {self.total_keys}',
            '# HELP opencode_keys_available Available keys',
            '# TYPE opencode_keys_available gauge',
            f'opencode_keys_available {self.available_keys}',
            '# HELP opencode_in_flight_total In flight',
            '# TYPE opencode_in_flight_total gauge',
            f'opencode_in_flight_total {self._in_flight_total}',
        ]
        for k in self.keys:
            st = k.stats(self.soft_limit, self.hard_limit)
            lines.append(f'opencode_key_rpm{{key="{k.label}"}} {st["current_rpm"]}')
            lines.append(f'opencode_key_blocked{{key="{k.label}"}} {1 if st["hard_blocked"] else 0}')
            lines.append(f'opencode_key_failures_total{{key="{k.label}"}} {st["total_failures"]}')
        return '\n'.join(lines) + '\n'

    def health_json(self) -> dict:
        return {
            'status': 'ok' if self.available_keys > 0 else 'degraded',
            'total_keys': self.total_keys,
            'available_keys': self.available_keys,
            'keys': self.all_stats(),
        }
