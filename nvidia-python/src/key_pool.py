#!/usr/bin/env python3
"""
key_pool.py — Two-tier rate-limited API key pool for NVIDIA NIM.
Migrated from key_pool.js — functionally identical with full pacing,
corroboration-based 429 classification, and FIFO admission queue.

Provides:
  - KeyPool class with acquire/release, rate limiting, pacing
  - KeyEntry class tracking per-key state
  - 429 classification (key-level vs model-level)
  - Model discovery (keyless-first)
  - Prometheus metrics export
  - Background key reload from .env
"""

import os
import time
import asyncio
import logging
from typing import Optional, List, Dict, Set, Tuple

import aiohttp

logger = logging.getLogger('wrapper-nvidia')

NVIDIA_BASE_URL = 'https://integrate.api.nvidia.com'
NVIDIA_GENAI_URL = 'https://ai.api.nvidia.com'
NVIDIA_NVCF_URL = 'https://api.nvcf.nvidia.com'


class Mutex:
    """Simple asyncio-based mutex."""

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
    """Tracks state for a single NVIDIA API key."""

    def __init__(self, label: str, api_key: str):
        self.label = label
        self.api_key = api_key
        self.soft_rpm: int = 30
        self.hard_rpm: int = 40
        self.timestamps: List[float] = []
        self.hard_blocked_until: float = 0.0
        self.model_blocks: Dict[str, float] = {}
        self.detected_limit: Optional[int] = None
        self.total_requests: int = 0
        self.total_429s: int = 0
        self.total_key_429s: int = 0
        self.total_model_429s: int = 0
        self.total_rotations_caused: int = 0
        self.last_used: float = 0.0
        self.last_admit: float = 0.0
        self.in_flight: int = 0

    @property
    def effective_load(self) -> int:
        return self.current_rpm() + self.in_flight

    def increment_in_flight(self):
        self.in_flight += 1

    def decrement_in_flight(self):
        if self.in_flight > 0:
            self.in_flight -= 1

    def admit_ready(self, interval: float) -> bool:
        if interval <= 0:
            return True
        return (time.time() - self.last_admit) >= interval

    def seconds_until_admit(self, interval: float) -> float:
        if interval <= 0:
            return 0.0
        return max(0.0, interval - (time.time() - self.last_admit))

    def current_rpm(self, window: int = 60) -> int:
        now = time.time()
        cutoff = now - window
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        return len(self.timestamps)

    def effective_hard_limit(self, configured_hard: int) -> int:
        hard = configured_hard or 40
        if self.detected_limit and self.detected_limit < hard:
            return self.detected_limit
        return hard

    def effective_soft_limit(self, configured_soft: int, configured_hard: int) -> int:
        hard = self.effective_hard_limit(configured_hard)
        soft = configured_soft or 30
        return max(1, min(soft, hard - 1))

    def seconds_until_below(self, limit: int, window: int = 60) -> float:
        now = time.time()
        ts = sorted([t for t in self.timestamps if now - t < window])
        rpm = len(ts)
        if rpm < limit:
            return 0.0
        idx = rpm - limit
        if idx < 0 or idx >= len(ts):
            return 0.0
        return max(0.0, window - (now - ts[idx]))

    def is_hard_blocked(self) -> bool:
        return time.time() < self.hard_blocked_until

    def is_model_blocked(self, model: str) -> bool:
        if not model:
            return False
        until = self.model_blocks.get(model)
        if not until:
            return False
        if time.time() < until:
            return True
        del self.model_blocks[model]
        return False

    def active_model_blocks(self) -> dict:
        now = time.time()
        out = {}
        for m, until in list(self.model_blocks.items()):
            rem = until - now
            if rem > 0:
                out[m] = round(rem * 10) / 10
            else:
                del self.model_blocks[m]
        return out

    def record(self):
        now = time.time()
        self.timestamps.append(now)
        self.total_requests += 1
        self.last_used = now

    def record_rate_limit(self, retry_after_sec: int = 65):
        self.hard_blocked_until = time.time() + retry_after_sec
        self.total_429s += 1
        self.total_key_429s += 1
        self.timestamps.append(time.time())

    def on_rate_limit(self, scope: str, model: str = None,
                      retry_after: int = None, detected_limit: int = None):
        now = time.time()
        self.total_429s += 1
        self.total_rotations_caused += 1
        self.timestamps.append(now)

        raw_secs = retry_after if retry_after else 8
        if scope == 'model' and model:
            block_secs = min(raw_secs, 10)
            self.model_blocks[model] = now + block_secs
            self.total_model_429s += 1
            logger.warning(f'[wrapper-nvidia] Key {self.label}: MODEL \'{model}\' rate-limited — blocked {block_secs}s')
        else:
            block_secs = min(raw_secs, 30)
            self.hard_blocked_until = now + block_secs
            self.total_key_429s += 1
            if detected_limit:
                old = self.detected_limit
                self.detected_limit = detected_limit
                if old != detected_limit:
                    logger.warning(f'[wrapper-nvidia] Key {self.label}: detected actual limit = {detected_limit} rpm')
            logger.warning(f'[wrapper-nvidia] Key {self.label}: KEY-LEVEL rate-limited — whole key blocked {block_secs}s')

    def stats(self, soft: int, hard: int) -> dict:
        rpm = self.current_rpm()
        eff_hard = self.effective_hard_limit(hard)
        eff_soft = self.effective_soft_limit(soft, hard)
        now = time.time()
        return {
            'label': self.label,
            'key_prefix': (self.api_key[:16] + '...') if self.api_key else 'unknown',
            'current_rpm': rpm,
            'in_flight': self.in_flight,
            'effective_load': self.effective_load,
            'configured_soft': soft,
            'configured_hard': hard,
            'effective_soft': eff_soft,
            'effective_hard': eff_hard,
            'detected_limit': self.detected_limit,
            'utilization_pct': round(rpm / eff_hard * 100, 1) if eff_hard else 0,
            'hard_blocked': self.is_hard_blocked(),
            'hard_blocked_remaining_s': round(max(0, self.hard_blocked_until - now), 1),
            'model_blocks': self.active_model_blocks(),
            'total_requests': self.total_requests,
            'total_429s': self.total_429s,
            'total_key_429s': self.total_key_429s,
            'total_model_429s': self.total_model_429s,
            'total_rotations_caused': self.total_rotations_caused,
            'last_used_ago_s': round(now - self.last_used, 1) if self.last_used else None,
            'last_admit_ago_s': round(now - self.last_admit, 2) if self.last_admit else None,
        }


class KeyPool:
    """Manages a pool of NVIDIA API keys with rotation, pacing, rate limiting."""

    def __init__(self):
        self.keys: List[KeyEntry] = []
        self.soft_limit: int = 30
        self.hard_limit: int = 40
        self.pacing: bool = True
        self.pacing_max_wait: float = 120
        self.queue_limit: float = 1.0
        self.max_queue_size: int = 500
        self._admit_interval: float = 0.0
        self._version: str = '8.6.5-py'
        self._lock = Mutex()
        self._recent_429: List[dict] = []
        self._model_ts: Dict[str, List[float]] = {}
        self._model_limit: Dict[str, int] = {}
        self._key_model_limit: Dict[str, int] = {}
        self._model_ts_by_key: Dict[str, List[float]] = {}
        self._rr_index: int = 0
        self._ticket_seq: int = 0
        self._waiting: Dict[int, str] = {}
        self._idx: int = 0
        self._models_cache: List[str] = []
        self._models_cache_ts: float = 0.0
        self._init_errors: List[str] = []
        self._agent: Optional[aiohttp.ClientSession] = None
        self._owns_agent: bool = True
        self._models_metadata: Dict[str, dict] = {}
        self._keyless_discovery_logged: bool = False
        self._in_flight: int = 0
        self._avg_latency_24h: int = 0
        self._exhaust_24h: int = 0

    def set_external_session(self, session: aiohttp.ClientSession):
        self._agent = session
        self._owns_agent = False

    def set_external_agent(self, session):
        self.set_external_session(session)

    def load_from_env(self):
        self.keys = []
        self._init_errors = []
        keys_seen: Set[str] = set()
        env_keys: List[str] = []

        for key_name, value in sorted(os.environ.items()):
            import re
            m = re.match(r'^NVIDIA_API_KEY(_\d+)?$', key_name)
            if not m:
                continue
            if not value or len(value) < 10:
                self._init_errors.append(f'{key_name}: empty or too short, skipped')
                continue
            if value in keys_seen:
                continue
            keys_seen.add(value)
            env_keys.append(value.strip())

        if env_keys:
            self.keys = [KeyEntry(f'key{i+1}', k) for i, k in enumerate(env_keys)]
        else:
            logger.warning('[wrapper-nvidia] No NVIDIA_API_KEY* found in environment '
                           '--- running in discovery-only mode. Inference requests will be rejected.')

        self.soft_limit = int(os.environ.get('SOFT_LIMIT_RPM', '30'))
        self.hard_limit = int(os.environ.get('HARD_LIMIT_RPM', '40'))
        self.queue_limit = float(os.environ.get('QUEUE_LIMIT', '4'))
        self.max_queue_size = int(os.environ.get('MAX_QUEUE_SIZE', '500'))
        self.pacing_max_wait = int(os.environ.get('PACING_MAX_WAIT', '120'))
        self._admit_interval = 1.0 / self.queue_limit if self.queue_limit > 0 else 0.0

        logger.info(f'[key_pool] Loaded {len(self.keys)} key(s) | soft={self.soft_limit} hard={self.hard_limit} rpm')
        return self

    def release_success(self, key: KeyEntry = None):
        """Compatibility release hook used by streaming proxy paths.

        KeyEntry in-flight counters are decremented by the server path that owns
        the stream. This method intentionally remains conservative so legacy
        callers that expect a pool-level release hook do not crash.
        """
        try:
            if key is not None:
                key.decrement_in_flight()
        except Exception:
            pass

    @property
    def total_keys(self) -> int:
        return len(self.keys)

    @property
    def available_keys(self) -> int:
        return sum(1 for k in self.keys if not k.is_hard_blocked() and k.current_rpm() < k.effective_hard_limit(self.hard_limit))

    def available_for_model(self, model_id: str) -> int:
        if not model_id:
            return self.available_keys
        return sum(1 for k in self.keys if not k.is_hard_blocked() and not k.is_model_blocked(model_id) and k.current_rpm() < k.effective_hard_limit(self.hard_limit))

    @property
    def blocked_keys(self) -> int:
        return sum(1 for k in self.keys if k.is_hard_blocked())

    @property
    def exhausted_count(self) -> int:
        return sum(1 for k in self.keys if k.current_rpm() >= k.effective_hard_limit(self.hard_limit))

    @property
    def total_soft_capacity(self) -> int:
        return sum(max(0, k.effective_soft_limit(self.soft_limit, self.hard_limit) - k.current_rpm()) for k in self.keys)

    # ── Per-model rate tracking ──────────────────────────────────────────
    def record_model(self, model: str, key_label: str = None):
        if not model:
            return
        now = time.time()
        window = 60
        if key_label:
            k = f'{key_label}/{model}'
            if k not in self._model_ts_by_key:
                self._model_ts_by_key[k] = []
            self._model_ts_by_key[k] = [t for t in self._model_ts_by_key[k] if now - t < window]
            self._model_ts_by_key[k].append(now)
        if model not in self._model_ts:
            self._model_ts[model] = []
        self._model_ts[model] = [t for t in self._model_ts[model] if now - t < window]
        self._model_ts[model].append(now)

    def model_rpm(self, model: str, window: int = 60) -> int:
        if not model:
            return 0
        now = time.time()
        lst = [t for t in (self._model_ts.get(model, [])) if now - t < window]
        self._model_ts[model] = lst
        return len(lst)

    def key_model_rpm(self, key_label: str, model: str, window: int = 60) -> int:
        if not key_label or not model:
            return 0
        now = time.time()
        k = f'{key_label}/{model}'
        lst = [t for t in (self._model_ts_by_key.get(k, [])) if now - t < window]
        self._model_ts_by_key[k] = lst
        return len(lst)

    def note_model_429(self, model: str, observed_rpm: int, key_label: str = None):
        if not model:
            return
        val = max(1, int(observed_rpm))
        if key_label:
            k = f'{key_label}/{model}'
            cur = self._key_model_limit.get(k)
            self._key_model_limit[k] = min(cur, val) if cur else val
        cur_g = self._model_limit.get(model)
        self._model_limit[model] = min(cur_g, val) if cur_g else val

    def model_limit(self, model: str, key_label: str = None) -> Optional[int]:
        if key_label:
            k = f'{key_label}/{model}'
            return self._key_model_limit.get(k)
        return self._model_limit.get(model)

    # ── 429 Classification ──────────────────────────────────────────────
    def _classify_429(self, state: KeyEntry, model: str, body_text: str,
                      rpm_at_429: int, eff_hard: int) -> Tuple[str, str]:
        txt = (body_text or '').lower()

        # Signal 1: prefer explicit KEY hints BEFORE the model-name substring test
        key_hints = os.environ.get('KEY_429_HINTS',
            'account rate limit,api key rate limit,organization rate limit,'
            'your key rate limit,credential rate limit,key quota exceeded,'
            'account quota exceeded').split(',')
        key_hints = [h.strip().lower() for h in key_hints if h.strip()]
        if any(h in txt for h in key_hints):
            return ('key', 'key-hint-in-body')

        model_hints = os.environ.get('MODEL_429_HINTS',
            'rate limit exceeded for model,model rate limit exceeded,'
            'per-model rate limit,requests for this model exceeded,'
            'model quota exceeded,model capacity exceeded').split(',')
        model_hints = [h.strip().lower() for h in model_hints if h.strip()]
        if any(h in txt for h in model_hints):
            return ('model', 'model-hint-in-body')

        if model and model.lower() in txt:
            return ('model', 'model-name-in-body')

        # Signal 2: corroboration
        now = time.time()
        window_s = int(os.environ.get('CORROBORATION_WINDOW_S', '60'))
        self._recent_429 = [item for item in self._recent_429 if now - item['ts'] < window_s]
        other_keys_for_model = set()
        other_models_for_key = set()
        for item in self._recent_429:
            if item['model'] == model and item['key_label'] != state.label:
                other_keys_for_model.add(item['key_label'])
            if item['key_label'] == state.label and item['model'] != model:
                other_models_for_key.add(item['model'])

        if other_models_for_key:
            return ('key', 'multi-model-on-key')
        if other_keys_for_model:
            return ('model', 'multi-key-for-model')

        # Signal 3: RPM ratio
        key_level_rpm_ratio = float(os.environ.get('KEY_LEVEL_RPM_RATIO', '0.8'))
        if eff_hard and rpm_at_429 >= eff_hard * key_level_rpm_ratio:
            return ('key', f'rpm-near-cap({rpm_at_429}/{eff_hard})')
        return ('model', f'rpm-low({rpm_at_429}/{eff_hard})')

    async def register_rate_limit(self, state: KeyEntry, model: str,
                                  retry_after: int, detected_limit: int = None,
                                  body_text: str = '') -> Tuple[str, str]:
        await self._lock.acquire()
        try:
            rpm = state.current_rpm()
            eff_hard = state.effective_hard_limit(self.hard_limit)
            scope, reason = self._classify_429(state, model, body_text, rpm, eff_hard)
            state.on_rate_limit(scope, model, retry_after, detected_limit)
            self._recent_429.append({'ts': time.time(), 'key_label': state.label, 'model': model})
            if scope == 'model' and model:
                self.note_model_429(model, self.model_rpm(model), state.label)
            return (scope, reason)
        finally:
            self._lock.release()

    # ── Selection & Pacing Queue ─────────────────────────────────────────
    async def acquire(self, model: str = '', signal=None) -> Optional[dict]:
        chosen, waited_s = await self._acquire_slot(model, signal)
        if chosen is None:
            return None
        return {'key': chosen, 'waited_ms': round(waited_s * 1000)}

    async def _acquire_slot(self, model: str = '', signal=None) -> Tuple[Optional[KeyEntry], float]:
        start = time.time()
        soft = self.soft_limit
        hard = self.hard_limit
        interval = self._admit_interval
        my_ticket: Optional[int] = None
        abort_promise = None

        # Load shedding check (safe outside lock)
        load_shedding_enabled = os.environ.get('LOAD_SHEDDING_ENABLED', 'true').lower() != 'false'
        inflight_soft_cap = int(os.environ.get('INFLIGHT_SOFT_CAP', '100'))
        if load_shedding_enabled:
            total_in_flight = sum(k.in_flight for k in self.keys)
            if total_in_flight >= inflight_soft_cap:
                logger.warning(f'[wrapper-nvidia] Load shedding: total in-flight {total_in_flight} >= INFLIGHT_SOFT_CAP {inflight_soft_cap}. Rejecting with 503.')
                return (None, 0.0)

        if signal is not None:
            if signal.is_set():
                return (None, 0.0)
            abort_promise = asyncio.Future()

            def _on_abort():
                if not abort_promise.done():
                    abort_promise.set_result(True)

            # For asyncio.Event, we can't easily add a callback, so we check
            # signal.is_set() in the loop instead.

        try:
            while True:
                if signal is not None and signal.is_set():
                    break

                await self._lock.acquire()
                sleep_duration = 0.0
                should_sleep = True
                try:
                    if my_ticket is None:
                        if len(self._waiting) >= self.max_queue_size:
                            logger.warning(f'[wrapper-nvidia] Queue backpressure load shed: waiting queue size {len(self._waiting)} exceeds max {self.max_queue_size}. Rejecting request.')
                            should_sleep = False
                            break
                        my_ticket = self._ticket_seq
                        self._ticket_seq += 1
                        self._waiting[my_ticket] = model

                    now = time.time()
                    avail = [s for s in self.keys if not s.is_hard_blocked() and not s.is_model_blocked(model)]

                    # Saturation checks
                    model_saturated = False
                    if model and avail:
                        all_saturated = True
                        for s in avail:
                            kml = self._key_model_limit.get(f'{s.label}/{model}')
                            if kml is not None and self.key_model_rpm(s.label, model) >= max(1, int(kml * 0.9)):
                                continue
                            else:
                                all_saturated = False
                                break
                        if all_saturated:
                            model_saturated = True

                    chosen = None
                    wait = None

                    if avail and not model_saturated:
                        idle_rpm = 3
                        def rpm_ok(s: KeyEntry, idle_rpm=idle_rpm) -> bool:
                            current = s.current_rpm()
                            if current < idle_rpm:
                                return True
                            lim = s.effective_soft_limit(soft, hard) if self.pacing else s.effective_hard_limit(hard)
                            return current < lim

                        def admit_ok(s: KeyEntry) -> bool:
                            return s.admit_ready(interval)

                        ready = [s for s in avail if rpm_ok(s) and admit_ok(s)]

                        if model:
                            ready = [s for s in ready if not (
                                self._key_model_limit.get(f'{s.label}/{model}') is not None and
                                self.key_model_rpm(s.label, model) >= max(1, int(self._key_model_limit[f'{s.label}/{model}'] * 0.9))
                            )]

                        # Per-model pacing rank
                        rank = sum(1 for t, m in self._waiting.items() if t < my_ticket and (model if model else True) == (m if m else None))

                        model_specific_shed = model and avail and all(
                            self._key_model_limit.get(f'{s.label}/{model}') is not None and
                            self.key_model_rpm(s.label, model) >= max(1, int(self._key_model_limit[f'{s.label}/{model}'] * 0.9))
                            for s in avail
                        )

                        if ready and (interval <= 0 or rank < len(ready)):
                            chosen = self._pick_key(ready)
                            chosen.record()
                            chosen.increment_in_flight()
                            chosen.last_admit = now
                            self.record_model(model, chosen.label)
                            del self._waiting[my_ticket]
                            my_ticket = None
                            should_sleep = False
                            return (chosen, time.time() - start)

                        waits = []
                        for s in avail:
                            rpm_w = s.seconds_until_below(s.effective_soft_limit(soft, hard))
                            adm_w = s.seconds_until_admit(interval)
                            waits.append(max(rpm_w, adm_w))
                        wait = min(waits) if waits else 1.0

                        if model_specific_shed:
                            wait = 1.0
                        elif not avail:
                            wait = 1.0
                    elif model_saturated:
                        wait = 1.0
                    else:
                        secs, _ = self._retry_hint(model)
                        wait = secs

                    wait = max(0.02, min(wait if wait is not None else 1.0, 5.0))

                    elapsed = time.time() - start
                    if elapsed >= self.pacing_max_wait:
                        should_sleep = False
                        break

                    sleep_duration = min(wait, self.pacing_max_wait - elapsed + 0.01)
                finally:
                    self._lock.release()

                if should_sleep:
                    if signal is not None:
                        try:
                            await asyncio.wait_for(signal.wait(), timeout=sleep_duration)
                            break  # signal was set
                        except asyncio.TimeoutError:
                            pass
                    else:
                        await asyncio.sleep(sleep_duration)

            if my_ticket is not None:
                del self._waiting[my_ticket]
            return (None, time.time() - start)
        finally:
            pass

    def _retry_hint(self, model: str = None) -> Tuple[int, str]:
        now = time.time()
        if self.keys and all(s.is_hard_blocked() for s in self.keys):
            secs = min(s.hard_blocked_until - now for s in self.keys)
            return (max(1, round(secs)), 'all_keys')

        live = [s for s in self.keys if not s.is_hard_blocked()]
        if model and live and all(s.is_model_blocked(model) for s in live):
            secs = min(s.model_blocks[model] - now for s in live if model in s.model_blocks)
            return (max(1, round(secs)), 'model')

        return (8, 'capacity')

    def _pick_key(self, ready: List[KeyEntry]) -> KeyEntry:
        load = {s.label: s.effective_load for s in ready}
        min_load = min(load.values())
        candidates = [s for s in ready if load[s.label] == min_load]
        if len(candidates) == 1:
            return candidates[0]
        labels = [s.label for s in self.keys]

        def rr_distance(s: KeyEntry) -> int:
            idx = labels.index(s.label)
            if idx == -1:
                return len(labels)
            return (idx - self._rr_index + len(labels)) % len(labels)

        candidates.sort(key=rr_distance)
        chosen = candidates[0]
        chosen_idx = labels.index(chosen.label)
        if chosen_idx != -1:
            self._rr_index = (chosen_idx + 1) % len(labels)
        else:
            self._rr_index = 0
        return chosen

    def peek_key(self) -> Optional[KeyEntry]:
        for s in self.keys:
            if not s.is_hard_blocked() and s.in_flight < 5:
                return s
        for s in self.keys:
            if not s.is_hard_blocked():
                return s
        return self.keys[0] if self.keys else None

    async def sync_keys(self, keys_list: List[str]) -> bool:
        if not keys_list:
            return False
        await self._lock.acquire()
        try:
            old_set = {k.api_key for k in self.keys}
            new_set = set(keys_list)

            same = old_set == new_set and len(self.keys) == len(keys_list)
            if same:
                for i, k in enumerate(keys_list):
                    if i < len(self.keys) and self.keys[i].api_key != k:
                        same = False
                        break

            if same:
                return False

            by_api = {k.api_key: k for k in self.keys}
            new_keys = []
            for i, k in enumerate(keys_list):
                ex = by_api.get(k)
                if ex:
                    ex.label = f'key{i+1}'
                    new_keys.append(ex)
                else:
                    new_keys.append(KeyEntry(f'key{i+1}', k))

            added = [k for k in keys_list if k not in old_set]
            removed = [k for k in old_set if k not in new_set]
            self.keys = new_keys
            logger.info(f'[wrapper-nvidia] Key pool synced: +{len(added)} / -{len(removed)} -> {len(self.keys)} total key(s)')
            return True
        finally:
            self._lock.release()

    async def sync_limits(self, limits: dict):
        await self._lock.acquire()
        try:
            if limits.get('soft') is not None and limits['soft'] != self.soft_limit:
                logger.info(f'[wrapper-nvidia] Soft limit synced: {self.soft_limit} -> {limits["soft"]} RPM')
                self.soft_limit = limits['soft']
            if limits.get('hard') is not None and limits['hard'] != self.hard_limit:
                logger.info(f'[wrapper-nvidia] Hard limit synced: {self.hard_limit} -> {limits["hard"]} RPM')
                self.hard_limit = limits['hard']
            if limits.get('queue_limit') is not None and limits['queue_limit'] != self.queue_limit:
                logger.info(f'[wrapper-nvidia] Queue limit synced: {self.queue_limit} -> {limits["queue_limit"]} QPS')
                self.queue_limit = limits['queue_limit']
                self._admit_interval = 1.0 / limits['queue_limit'] if limits['queue_limit'] > 0 else 0.0
            if limits.get('max_queue_size') is not None and limits['max_queue_size'] != self.max_queue_size:
                logger.info(f'[wrapper-nvidia] Max queue size synced: {self.max_queue_size} -> {limits["max_queue_size"]}')
                self.max_queue_size = limits['max_queue_size']
        finally:
            self._lock.release()

    def blocked_models(self) -> dict:
        now = time.time()
        agg = {}
        for s in self.keys:
            for m, until in s.model_blocks.items():
                rem = until - now
                if rem <= 0:
                    continue
                if m not in agg:
                    agg[m] = {'keys': [], 'retry_s': rem}
                agg[m]['keys'].append(s.label)
                agg[m]['retry_s'] = min(agg[m]['retry_s'], rem)
        for m in agg:
            agg[m]['retry_s'] = round(agg[m]['retry_s'] * 10) / 10
        return agg

    async def reset_counters(self):
        await self._lock.acquire()
        try:
            for s in self.keys:
                s.total_requests = 0
                s.total_429s = 0
                s.total_key_429s = 0
                s.total_model_429s = 0
                s.total_rotations_caused = 0
                s.in_flight = 0
            self._recent_429 = []
            logger.info('[wrapper-nvidia] Per-key cumulative counters reset')
        finally:
            self._lock.release()

    async def heal_in_flight(self):
        await self._lock.acquire()
        try:
            now = time.time()
            total_fixed = 0
            threshold = int(os.environ.get('HEAL_INFLIGHT_THRESHOLD_SEC', '600'))
            for s in self.keys:
                if s.in_flight > 0 and s.last_used > 0 and (now - s.last_used) > threshold:
                    logger.warning(f'[wrapper-nvidia] heal_in_flight: {s.label} in_flight {s.in_flight} stuck since lastUsed {round(now - s.last_used)}s ago -> 0')
                    s.in_flight = 0
                    total_fixed += 1
                elif s.in_flight > 0 and s.last_used == 0:
                    logger.warning(f'[wrapper-nvidia] heal_in_flight: {s.label} in_flight {s.in_flight} with no lastUsed -> 0')
                    s.in_flight = 0
                    total_fixed += 1
            if total_fixed:
                logger.info(f'[wrapper-nvidia] heal_in_flight: {total_fixed} key(s) corrected')
        finally:
            self._lock.release()

    def all_stats(self) -> list:
        return [s.stats(self.soft_limit, self.hard_limit) for s in self.keys]

    def key_details(self) -> list:
        return [
            {
                'label': k.label,
                'soft_rpm': k.soft_rpm,
                'hard_rpm': k.hard_rpm,
                'soft_used': k.current_rpm(),
                'hard_used': k.current_rpm(),
                'soft_available': k.effective_load < k.effective_soft_limit(self.soft_limit, self.hard_limit),
                'hard_available': k.effective_load < k.effective_hard_limit(self.hard_limit),
                'ready': not k.is_hard_blocked(),
                'pacing_ready': k.admit_ready(self._admit_interval),
                'cooldown_until': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(k.hard_blocked_until)) if k.hard_blocked_until else None,
                'exhaustions': k.total_key_429s,
                'total_requests': k.total_requests,
            }
            for k in self.keys
        ]

    def summary(self) -> dict:
        stats = self.all_stats()
        return {
            'total_keys': len(stats),
            'available_keys': sum(1 for s in self.keys if not s.is_hard_blocked() and s.current_rpm() < s.effective_hard_limit(self.hard_limit)),
            'blocked_models': self.blocked_models(),
            'learned_model_limits': self._model_limit,
            'learned_key_model_limits': self._key_model_limit,
        }

    def prom_metrics(self) -> str:
        lines = [
            '# HELP wrapper_nvidia_keys_total Total API keys loaded',
            '# TYPE wrapper_nvidia_keys_total gauge',
            f'wrapper_nvidia_keys_total {self.total_keys}',
            '# HELP wrapper_nvidia_keys_available Keys with soft RPM capacity',
            '# TYPE wrapper_nvidia_keys_available gauge',
            f'wrapper_nvidia_keys_available {self.available_keys}',
            '# HELP wrapper_nvidia_keys_blocked Keys blocked (exhausted/cooldown)',
            '# TYPE wrapper_nvidia_keys_blocked gauge',
            f'wrapper_nvidia_keys_blocked {self.blocked_keys}',
            '# HELP wrapper_nvidia_rpm_total Total virtual RPM capacity across all keys',
            '# TYPE wrapper_nvidia_rpm_total gauge',
            f'wrapper_nvidia_rpm_total {self.total_soft_capacity}',
            '# HELP wrapper_nvidia_in_flight_total Currently processing requests',
            '# TYPE wrapper_nvidia_in_flight_total gauge',
            f'wrapper_nvidia_in_flight_total {self._in_flight}',
            '# HELP wrapper_nvidia_avg_latency_ms_24h Average latency last 24h',
            '# TYPE wrapper_nvidia_avg_latency_ms_24h gauge',
            f'wrapper_nvidia_avg_latency_ms_24h {self._avg_latency_24h}',
            '# HELP wrapper_nvidia_exhaustions_total_24h Key exhaustion events last 24h',
            '# TYPE wrapper_nvidia_exhaustions_total_24h gauge',
            f'wrapper_nvidia_exhaustions_total_24h {self._exhaust_24h}',
            '# HELP wrapper_nvidia_models_cached Number of cached model IDs',
            '# TYPE wrapper_nvidia_models_cached gauge',
            f'wrapper_nvidia_models_cached {len(self._models_cache)}',
        ]
        for s in self.all_stats():
            label = s.get('label', 'unknown')
            lines.append(f'wrapper_nvidia_key_rpm{{key="{label}"}} {s.get("current_rpm", 0)}')
            lines.append(f'wrapper_nvidia_key_in_flight{{key="{label}"}} {s.get("in_flight", 0)}')
            lines.append(f'wrapper_nvidia_key_hard_blocked{{key="{label}"}} {1 if s.get("hard_blocked") else 0}')
            lines.append(f'wrapper_nvidia_key_unused_429_total{{key="{label}"}} {s.get("total_429s", 0)}')
        return '\n'.join(lines) + '\n'

    def health_json(self) -> dict:
        return {
            'status': 'ok' if self.available_keys > 0 else 'degraded',
            'total_keys': self.total_keys,
            'available_keys': self.available_keys,
            'blocked_keys': self.blocked_keys,
            'soft_limit_rpm': self.soft_limit,
            'hard_limit_rpm': self.hard_limit,
            'queue_limit_per_key_per_sec': self.queue_limit,
            'models_cached': len(self._models_cache),
            'version': self._version,
        }

    # ── Model Discovery ───────────────────────────────────────────────────
    async def _fetch_models(self) -> List[str]:
        base_url = os.environ.get('NVIDIA_BASE_URL', NVIDIA_BASE_URL).rstrip('/')

        # Keyless-first discovery
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f'{base_url}/v1/models',
                                       headers={'Accept': 'application/json'}) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        models_raw = body.get('data', body.get('models', []))
                        parsed = []
                        for m in models_raw:
                            mid = m if isinstance(m, str) else m.get('id', '')
                            if not mid:
                                continue
                            clean_id = mid.replace('stg/', '').replace('dev/', '').replace('test/', '')
                            parsed.append(clean_id)
                            if isinstance(m, dict):
                                self._models_metadata[clean_id] = m
                        if parsed:
                            parsed.sort()
                            if not self._keyless_discovery_logged:
                                logger.info(f'[wrapper-nvidia] Model catalog fetched KEYLESS from {base_url}/v1/models ({len(parsed)} models)')
                                self._keyless_discovery_logged = True
                            return parsed
        except Exception as e:
            logger.warning(f'[wrapper-nvidia] Keyless /v1/models failed ({e}); falling back to keyed fetch')

        # Keyed fallback
        key = self.peek_key()
        if not key:
            logger.warning('[wrapper-nvidia] No key available for model-discovery fallback; serving cached/empty list')
            return []

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f'{base_url}/v1/models',
                                       headers={'Authorization': f'Bearer {key.api_key}', 'Accept': 'application/json'}) as resp:
                    if resp.status != 200:
                        return []
                    body = await resp.json()
                    models_raw = body.get('data', body.get('models', []))
                    parsed = []
                    for m in models_raw:
                        mid = m if isinstance(m, str) else m.get('id', '')
                        if not mid:
                            continue
                        clean_id = mid.replace('stg/', '').replace('dev/', '').replace('test/', '')
                        parsed.append(clean_id)
                        if isinstance(m, dict):
                            self._models_metadata[clean_id] = m
                    parsed.sort()
                    return parsed
        except Exception:
            return []

    async def refresh_models(self, force: bool = False) -> List[str]:
        if not force and self._models_cache:
            return self._models_cache

        models = await self._fetch_models()
        if models:
            self._models_cache = models
            self._models_cache_ts = time.time()
        return self._models_cache

    @property
    def models_cached(self) -> List[str]:
        return self._models_cache

    @property
    def models_metadata(self) -> dict:
        return self._models_metadata

    def prune_stale_entries(self):
        now = time.time()
        window = 60
        for model in list(self._model_ts.keys()):
            self._model_ts[model] = [t for t in self._model_ts[model] if now - t < window]
            if not self._model_ts[model]:
                del self._model_ts[model]
        for k in list(self._model_ts_by_key.keys()):
            self._model_ts_by_key[k] = [t for t in self._model_ts_by_key[k] if now - t < window]
            if not self._model_ts_by_key[k]:
                del self._model_ts_by_key[k]
        self._recent_429 = [item for item in self._recent_429 if now - item['ts'] < 60]
        for model in list(self._model_limit.keys()):
            ts = [t for t in self._model_ts.get(model, []) if now - t < 600]
            if not ts:
                del self._model_limit[model]
        for km in list(self._key_model_limit.keys()):
            key_label, model = km.split('/')
            k = f'{key_label}/{model}'
            ts = [t for t in self._model_ts_by_key.get(k, []) if now - t < 600]
            if not ts:
                del self._key_model_limit[km]

    def start_model_refresh(self):
        refresh_sec = int(os.environ.get('MODEL_REFRESH_SEC', '600'))
        if refresh_sec <= 0:
            return

        async def _refresh_loop():
            while True:
                await asyncio.sleep(refresh_sec)
                try:
                    await self.refresh_models(force=True)
                    self.prune_stale_entries()
                except Exception as e:
                    logger.error(f'[key_pool] Model refresh loop error: {e}')

        asyncio.create_task(_refresh_loop())
