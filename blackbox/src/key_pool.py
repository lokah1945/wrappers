#!/usr/bin/env python3
"""Resilient multi-key pool for BLACKBOX AI."""

from __future__ import annotations

import os
import time
import asyncio
import logging
from typing import Optional, List

logger = logging.getLogger('wrapper-blackbox')


class Mutex:
    def __init__(self):
        self._queue: List[asyncio.Future] = []
        self._locked = False

    async def acquire(self):
        if not self._locked:
            self._locked = True
            return
        fut = asyncio.get_event_loop().create_future()
        self._queue.append(fut)
        await fut

    def release(self):
        if self._queue:
            fut = self._queue.pop(0)
            if not fut.done():
                fut.set_result(None)
        else:
            self._locked = False


class KeyEntry:
    def __init__(self, label: str, api_key: str):
        self.label = label
        self.api_key = api_key
        self.soft_rpm = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_rpm = int(os.environ.get('HARD_LIMIT_RPM', '40'))
        self.timestamps: List[float] = []
        self.hard_blocked_until = 0.0
        self.block_reason = ''
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
            self.block_reason = ''
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
        seconds = max(1, min(int(seconds or 1), int(os.environ.get('KEY_COOLDOWN_MAX_SEC', '300'))))
        self.hard_blocked_until = max(self.hard_blocked_until, time.time() + seconds)
        self.block_reason = reason
        self.total_failures += 1
        if reason == 'rate_limit':
            self.total_429s += 1
        logger.warning(f'[blackbox] key {self.label} cooled down for {seconds}s ({reason})')

    def stats(self) -> dict:
        return {
            'label': self.label,
            'current_rpm': self.current_rpm(),
            'in_flight': self.in_flight,
            'effective_load': self.effective_load,
            'hard_blocked': self.is_blocked(),
            'hard_blocked_remaining_s': max(0, round(self.hard_blocked_until - time.time(), 1)),
            'block_reason': self.block_reason or None,
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
            'total_failures': self.total_failures,
            'soft_rpm': self.soft_rpm,
            'hard_rpm': self.hard_rpm,
        }


class KeyPool:
    def __init__(self):
        self.keys: List[KeyEntry] = []
        self.soft_limit = 30
        self.hard_limit = 40
        self._lock = Mutex()
        self._rr = 0
        self._in_flight_total = 0

    def load_from_env(self):
        env_keys = []
        seen = set()
        for key_name, value in sorted(os.environ.items()):
            if key_name == 'BLACKBOX_API_KEY' or key_name.startswith('BLACKBOX_API_KEY_'):
                v = (value or '').strip()
                if len(v) < 10 or v in seen:
                    continue
                seen.add(v)
                env_keys.append(v)
        self.soft_limit = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_limit = int(os.environ.get('HARD_LIMIT_RPM', '40'))
        self.keys = [KeyEntry(f'key{i+1}', k) for i, k in enumerate(env_keys)]
        self._rr = 0
        if not self.keys:
            logger.warning('[blackbox] No BLACKBOX_API_KEY* found')
        logger.info(f'[blackbox] Loaded {len(self.keys)} key(s) soft={self.soft_limit} hard={self.hard_limit}')
        return self

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_blocked() and k.current_rpm() < (k.hard_rpm or self.hard_limit))

    async def acquire(self, model: str = '') -> Optional[dict]:
        await self._lock.acquire()
        try:
            inflight_cap = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))
            if sum(k.in_flight for k in self.keys) >= inflight_cap:
                logger.warning(f'[blackbox] Load shedding: in-flight >= {inflight_cap}')
                return None
            candidates = [k for k in self.keys if not k.is_blocked() and k.current_rpm() < (k.hard_rpm or self.hard_limit)]
            if not candidates:
                return None
            min_load = min(k.effective_load for k in candidates)
            best = [k for k in candidates if k.effective_load == min_load]
            key = best[self._rr % len(best)]
            self._rr += 1
            key.record()
            self._in_flight_total += 1
            return {'key': key}
        finally:
            self._lock.release()

    def release(self, key: KeyEntry = None):
        if key is None:
            return
        key.release()
        self._in_flight_total = max(0, self._in_flight_total - 1)

    def mark_failure(self, key: KeyEntry, status_code: int = 0, retry_after: int = None, reason: str = ''):
        if key is None:
            return
        if status_code == 429:
            key.block(retry_after or int(os.environ.get('RATE_LIMIT_COOLDOWN_SEC', '65')), 'rate_limit')
        elif status_code in (401, 402, 403):
            key.block(retry_after or int(os.environ.get('AUTH_KEY_COOLDOWN_SEC', '300')), 'auth_or_quota')
        elif status_code >= 500 or status_code in (408, 409):
            key.block(retry_after or int(os.environ.get('TRANSIENT_KEY_COOLDOWN_SEC', '15')), 'transient')
        elif reason:
            key.block(retry_after or 15, reason)

    def all_stats(self) -> list:
        return [k.stats() for k in self.keys]

    def prom_metrics(self) -> str:
        lines = [
            '# HELP blackbox_keys_total Total keys',
            '# TYPE blackbox_keys_total gauge',
            f'blackbox_keys_total {self.total_keys}',
            '# HELP blackbox_keys_available Available keys',
            '# TYPE blackbox_keys_available gauge',
            f'blackbox_keys_available {self.available_keys}',
            '# HELP blackbox_in_flight_total In flight',
            '# TYPE blackbox_in_flight_total gauge',
            f'blackbox_in_flight_total {self._in_flight_total}',
        ]
        for k in self.keys:
            st = k.stats()
            lines.append(f'blackbox_key_rpm{{key="{k.label}"}} {st["current_rpm"]}')
            lines.append(f'blackbox_key_blocked{{key="{k.label}"}} {1 if st["hard_blocked"] else 0}')
            lines.append(f'blackbox_key_failures_total{{key="{k.label}"}} {st["total_failures"]}')
        return '\n'.join(lines) + '\n'

    def health_json(self) -> dict:
        return {
            'status': 'ok' if self.available_keys > 0 else 'degraded',
            'total_keys': self.total_keys,
            'available_keys': self.available_keys,
            'keys': self.all_stats(),
        }
