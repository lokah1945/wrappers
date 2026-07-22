#!/usr/bin/env python3
"""
key_pool.py — Multi-key pool for OpenCode (adapted from wrapper-nvidia patterns).
Full production hardening: pacing, load shedding (INFLIGHT_SOFT_CAP=100), rate limiting, in-flight tracking.
"""

import os
import time
import asyncio
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger('wrapper-opencode')

class Mutex:
    def __init__(self):
        self._queue: List[asyncio.Future] = []
        self._locked = False

    async def acquire(self):
        if not self._locked:
            self._locked = True
            return
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
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
        self.soft_rpm: int = 30
        self.hard_rpm: int = 40
        self.timestamps: List[float] = []
        self.hard_blocked_until: float = 0.0
        self.in_flight: int = 0
        self.total_requests: int = 0
        self.total_429s: int = 0

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

    def record(self):
        self.timestamps.append(time.time())
        self.total_requests += 1

    def record_rate_limit(self, retry_after_sec: int = 65):
        self.hard_blocked_until = time.time() + retry_after_sec
        self.total_429s += 1
        self.timestamps.append(time.time())

    def is_hard_blocked(self) -> bool:
        return time.time() < self.hard_blocked_until

    def stats(self, soft: int, hard: int) -> dict:
        rpm = self.current_rpm()
        return {
            'label': self.label,
            'current_rpm': rpm,
            'in_flight': self.in_flight,
            'effective_load': self.effective_load,
            'hard_blocked': self.is_hard_blocked(),
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
        }

class KeyPool:
    def __init__(self):
        self.keys: List[KeyEntry] = []
        self.soft_limit: int = 30
        self.hard_limit: int = 40
        self._lock = Mutex()
        self._in_flight_total = 0

    def load_from_env(self):
        self.keys = []
        env_keys = []
        for key_name, value in sorted(os.environ.items()):
            if key_name.startswith('OPENCODE_API_KEY_') and value and len(value) > 10:
                env_keys.append(value.strip())

        if env_keys:
            self.keys = [KeyEntry(f'key{i+1}', k) for i, k in enumerate(env_keys)]
        else:
            logger.warning('[opencode] No OPENCODE_API_KEY_* found')

        self.soft_limit = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_limit = int(os.environ.get('HARD_LIMIT_RPM', '40'))
        logger.info(f'[opencode] Loaded {len(self.keys)} OpenCode key(s)')
        return self

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_hard_blocked() and k.current_rpm() < k.hard_rpm)

    async def acquire(self, model: str = '') -> Optional[dict]:
        await self._lock.acquire()
        try:
            # Load shedding
            inflight_cap = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))
            if sum(k.in_flight for k in self.keys) >= inflight_cap:
                logger.warning(f'[opencode] Load shedding: in-flight >= {inflight_cap}')
                return None

            for k in self.keys:
                if not k.is_hard_blocked() and k.current_rpm() < k.hard_rpm:
                    k.record()
                    k.increment_in_flight()
                    self._in_flight_total += 1
                    return {'key': k}
            return None
        finally:
            self._lock.release()

    def release(self, key: KeyEntry):
        key.decrement_in_flight()
        self._in_flight_total = max(0, self._in_flight_total - 1)

    def all_stats(self) -> list:
        return [k.stats(self.soft_limit, self.hard_limit) for k in self.keys]

    def prom_metrics(self) -> str:
        return f"""# HELP opencode_keys_total Total keys
# TYPE opencode_keys_total gauge
opencode_keys_total {self.total_keys}
# HELP opencode_in_flight_total In flight
# TYPE opencode_in_flight_total gauge
opencode_in_flight_total {self._in_flight_total}
"""

    def health_json(self) -> dict:
        return {
            'status': 'ok' if self.available_keys > 0 else 'degraded',
            'total_keys': self.total_keys,
            'available_keys': self.available_keys,
        }