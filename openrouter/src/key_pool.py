#!/usr/bin/env python3
"""
key_pool.py — resilient multi-key pool for OpenRouter.

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

logger = logging.getLogger('wrapper-openrouter')


class KeyEntry:
    """State for one OpenRouter credential."""

    def __init__(self, label: str, api_key: str):
        self.label = label
        self.api_key = api_key
        self.soft_rpm: int = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_rpm: int = int(os.environ.get('HARD_LIMIT_RPM', '60'))
        self.timestamps: List[float] = []
        self.hard_blocked_until: float = 0.0
        self.block_reason: str = ''
        self.in_flight: int = 0
        self.total_requests: int = 0
        self.total_429s: int = 0
        self.total_failures: int = 0
        self.last_used: float = 0.0
        self.model_blocked_until: dict = {}

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
        logger.warning(f'[openrouter] key {self.label} cooled down for {seconds}s ({reason})')

    def block_model(self, model_id: str, seconds: int, reason: str):
        """N-12 fix: cool down only this key+model pair (model-scoped failure)."""
        if not model_id:
            return
        seconds = max(1, min(int(seconds or 1), int(os.environ.get('KEY_COOLDOWN_MAX_SEC', '300'))))
        self.model_blocked_until[model_id] = max(
            self.model_blocked_until.get(model_id, 0.0), time.time() + seconds
        )
        self.total_failures += 1
        logger.warning(f'[openrouter] key {self.label} model {model_id!r} cooled down for {seconds}s ({reason})')

    def is_hard_blocked(self) -> bool:
        now = time.time()
        if now < self.hard_blocked_until:
            return True
        if self.hard_blocked_until:
            self.hard_blocked_until = 0.0
            self.block_reason = ''
        return False

    def is_model_blocked(self, model_id: str) -> bool:
        if not model_id:
            return False
        until = self.model_blocked_until.get(model_id, 0.0)
        if not until:
            return False
        now = time.time()
        if now < until:
            return True
        self.model_blocked_until.pop(model_id, None)
        return False

    def stats(self, soft: int, hard: int) -> dict:
        rpm = self.current_rpm()
        return {
            'label': self.label,
            'current_rpm': rpm,
            'in_flight': self.in_flight,
            'effective_load': self.effective_load,
            'hard_blocked': self.is_hard_blocked(),
            'hard_blocked_remaining_s': max(0, round(self.hard_blocked_until - time.time(), 1)),
            'block_reason': self.block_reason or None,
            'model_blocks': {
                m: round(u - time.time(), 1)
                for m, u in self.model_blocked_until.items()
                if u > time.time()
            },
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
            'total_failures': self.total_failures,
            'soft_rpm': self.soft_rpm or soft,
            'hard_rpm': self.hard_rpm or hard,
        }


class KeyPool:
    """Manages multiple OpenRouter API keys with rotation, cooldown and in-flight tracking."""

    def __init__(self):
        self.keys: List[KeyEntry] = []
        self.soft_limit: int = 30
        self.hard_limit: int = 60
        self._lock = asyncio.Lock()
        self._in_flight_total = 0
        self._rr = 0

    def load_from_env(self):
        """Load all OPENROUTER_API_KEY* from environment."""
        self.keys = []
        env_keys = []
        seen = set()
        for key_name, value in sorted(os.environ.items()):
            if not value or len(value.strip()) < 10:
                continue
            if key_name == 'OPENROUTER_API_KEY' or key_name.startswith('OPENROUTER_API_KEY_'):
                v = value.strip()
                if v in seen:
                    continue
                seen.add(v)
                env_keys.append(v)

        self.soft_limit = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_limit = int(os.environ.get('HARD_LIMIT_RPM', '60'))

        if env_keys:
            self.keys = [KeyEntry(f'key{i+1}', k) for i, k in enumerate(env_keys)]
        else:
            logger.warning('[openrouter] No OPENROUTER_API_KEY* found')

        logger.info(f'[openrouter] Loaded {len(self.keys)} OpenRouter key(s) soft={self.soft_limit} hard={self.hard_limit}')
        return self

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(
            1 for k in self.keys
            if not k.is_hard_blocked() and k.current_rpm() < (k.hard_rpm or self.hard_limit)
        )

    async def acquire(self, model: str = '') -> Optional[dict]:
        """Acquire the best available key for a request."""
        async with self._lock:
            inflight_cap = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))
            if sum(k.in_flight for k in self.keys) >= inflight_cap:
                logger.warning(f'[openrouter] Load shedding: in-flight >= {inflight_cap}')
                return None

            candidates = [
                k for k in self.keys
                if not k.is_hard_blocked()
                and not (model and k.is_model_blocked(model))
                and k.current_rpm() < (k.hard_rpm or self.hard_limit)
            ]
            if not candidates:
                return None

            min_load = min(k.effective_load for k in candidates)
            best = [k for k in candidates if k.effective_load == min_load]
            key = best[self._rr % len(best)]
            self._rr += 1
            key.record()
            key.increment_in_flight()
            self._in_flight_total += 1
            return {'key': key}

    def release(self, key: KeyEntry = None):
        """Release in-flight slot for a key."""
        if key is None:
            return
        key.decrement_in_flight()
        self._in_flight_total = max(0, self._in_flight_total - 1)

    def mark_failure(self, key: KeyEntry, status_code: int = 0, retry_after: int = None,
                     reason: str = '', available_keys: int = None, model: str = ''):
        """Mark a key failure with appropriate cooldown."""
        if key is None:
            return
        if status_code == 429:
            cooldown = retry_after or int(os.environ.get('RATE_LIMIT_COOLDOWN_SEC', '65'))
            key.block(cooldown, 'rate_limit')
        elif status_code in (401, 403, 402):
            cooldown = retry_after or int(os.environ.get('AUTH_KEY_COOLDOWN_SEC', '300'))
            key.block(cooldown, 'auth_or_quota')
        elif status_code >= 500 or status_code in (408, 409):
            cooldown = retry_after or int(os.environ.get('TRANSIENT_KEY_COOLDOWN_SEC', '15'))
            if available_keys is not None and available_keys <= 0 and cooldown > 1:
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

    def all_stats(self) -> list:
        return [k.stats(self.soft_limit, self.hard_limit) for k in self.keys]

    def prom_metrics(self) -> str:
        lines = [
            '# HELP openrouter_keys_total Total keys',
            '# TYPE openrouter_keys_total gauge',
            f'openrouter_keys_total {self.total_keys}',
            '# HELP openrouter_keys_available Available keys',
            '# TYPE openrouter_keys_available gauge',
            f'openrouter_keys_available {self.available_keys}',
            '# HELP openrouter_in_flight_total In flight',
            '# TYPE openrouter_in_flight_total gauge',
            f'openrouter_in_flight_total {self._in_flight_total}',
        ]
        for k in self.keys:
            st = k.stats(self.soft_limit, self.hard_limit)
            lines.append(f'openrouter_key_rpm{{key="{k.label}"}} {st["current_rpm"]}')
            lines.append(f'openrouter_key_blocked{{key="{k.label}"}} {1 if st["hard_blocked"] else 0}')
            lines.append(f'openrouter_key_failures_total{{key="{k.label}"}} {st["total_failures"]}')
        return '\n'.join(lines) + '\n'

    def health_json(self) -> dict:
        return {
            'status': 'ok' if self.available_keys > 0 else 'degraded',
            'total_keys': self.total_keys,
            'available_keys': self.available_keys,
            'keys': self.all_stats(),
        }
