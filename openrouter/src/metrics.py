#!/usr/bin/env python3
"""Persistent metrics for wrapper-openrouter.

JSON-file persistence on shutdown/restart, matching the reliability
guarantees of nvidia's SQLite-backed metrics.
"""

import json
import os
import threading
import time
from collections import deque
from pathlib import Path


class Metrics:
    """Thread-safe request metrics with disk persistence.

    DASH fix (2026-10-14): per-model / per-key / hourly / activity tracking
    added so the dashboard's Models card and /metrics/models endpoint carry
    real data. record_tokens() lets stream/non-stream paths add usage at
    completion WITHOUT double-counting the request itself (the request was
    already counted by record_request at response start)."""

    def __init__(self, db_path: str | None = None):
        self.start = time.time()
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self._db_path = db_path
        self._lock = threading.Lock()
        # DASH fix: dashboard dimensions.
        # deque is imported lazily so AST-extracted sandboxes (tests) that do
        # not carry module imports still can construct the class.
        from collections import deque as _deque
        self.per_model: dict = {}
        self.per_key: dict = {}
        self.hourly: dict = {}
        self.activity = _deque(maxlen=500)
        # B-39 fix: persist periodically, not only on graceful shutdown, so
        # counters survive SIGKILL/OOM (blackbox BB-15/OC-14 parity). This
        # matters more here because openrouter previously had no graceful
        # shutdown at all (B-34).
        self._persist_interval = float(os.environ.get('METRICS_PERSIST_SEC', '60'))
        self._last_persist = time.time()
        self._load_persisted()

    def _persist_path(self) -> str:
        if os.environ.get('METRICS_SNAPSHOT_FILE'):
            return os.environ['METRICS_SNAPSHOT_FILE']
        if self._db_path:
            return str(self._db_path).replace('.db', '-metrics.json')
        return str(Path(__file__).resolve().parents[1] / 'metrics-snapshot.json')

    def _load_persisted(self):
        """Load last known counters from disk (survive restarts)."""
        try:
            path = self._persist_path()
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                self.requests = int(data.get('requests', 0))
                self.tokens_in = int(data.get('tokens_in', 0))
                self.tokens_out = int(data.get('tokens_out', 0))
                self.errors = int(data.get('errors', 0))
                if isinstance(data.get('per_model'), dict):
                    self.per_model = data['per_model']
                if isinstance(data.get('per_key'), dict):
                    self.per_key = data['per_key']
                if isinstance(data.get('hourly'), dict):
                    self.hourly = data['hourly']
                if isinstance(data.get('activity'), list):
                    self.activity.extend(data['activity'][-self.activity.maxlen:])
        except Exception:
            pass

    def _persist(self):
        """Save current counters to disk."""
        try:
            path = self._persist_path()
            with open(path, 'w') as f:
                json.dump({
                    'requests': self.requests,
                    'tokens_in': self.tokens_in,
                    'tokens_out': self.tokens_out,
                    'errors': self.errors,
                    'per_model': self.per_model,
                    'per_key': self.per_key,
                    'hourly': self.hourly,
                    'activity': list(self.activity),
                    'saved_at': time.time(),
                }, f)
        except Exception:
            pass

    async def init(self):
        pass

    async def record_request(self, model: str = "", prompt_tokens: int = 0,
                              completion_tokens: int = 0, **kwargs):
        now = time.time()
        status_code = int(kwargs.get('status_code', 200) or 0)
        key_label = str(kwargs.get('key_label', '') or '')
        raw_latency = kwargs.get('latency_ms')
        try:
            latency_ms = max(0.0, float(raw_latency)) if raw_latency is not None else None
        except (TypeError, ValueError):
            latency_ms = None
        is_error = status_code >= 400
        with self._lock:
            self.requests += 1
            self.tokens_in += prompt_tokens
            self.tokens_out += completion_tokens
            if is_error:
                self.errors += 1
            if model:
                m = self.per_model.setdefault(model, {
                    'requests': 0, 'errors': 0, 'success_count': 0,
                    'tokens_in': 0, 'tokens_out': 0,
                    'latency_total_ms': 0.0, 'latency_count': 0,
                    'last_latency_ms': None, 'max_latency_ms': 0.0,
                    'first_ts': now, 'last_ts': now, 'last_status': status_code,
                })
                m['requests'] += 1
                if is_error:
                    m['errors'] += 1
                else:
                    m['success_count'] += 1
                m['tokens_in'] += prompt_tokens
                m['tokens_out'] += completion_tokens
                if latency_ms is not None:
                    m['latency_total_ms'] = m.get('latency_total_ms', 0.0) + latency_ms
                    m['latency_count'] = m.get('latency_count', 0) + 1
                    m['last_latency_ms'] = latency_ms
                    m['max_latency_ms'] = max(m.get('max_latency_ms', 0.0), latency_ms)
                m['last_ts'] = now
                m['last_status'] = status_code
            if key_label:
                k = self.per_key.setdefault(key_label, {
                    'requests': 0, 'errors': 0, 'rate_limited': 0,
                    'tokens_in': 0, 'tokens_out': 0, 'last_used_ts': now,
                })
                k['requests'] += 1
                if is_error:
                    k['errors'] += 1
                if status_code == 429:
                    k['rate_limited'] += 1
                k['tokens_in'] += prompt_tokens
                k['tokens_out'] += completion_tokens
                k['last_used_ts'] = now
            hour = int(now // 3600 * 3600)
            h = self.hourly.setdefault(hour, {
                'requests': 0, 'errors': 0, 'rate_limited': 0, 'tokens': 0,
            })
            h['requests'] += 1
            if is_error:
                h['errors'] += 1
            if status_code == 429:
                h['rate_limited'] += 1
            h['tokens'] += prompt_tokens + completion_tokens
            self.activity.append({
                'ts': int(now), 'id': self.requests,
                'model': model or 'unknown',
                'key_label': key_label or '—', 'status_code': status_code,
                'prompt_tokens': int(prompt_tokens),
                'completion_tokens': int(completion_tokens),
                'latency_ms': latency_ms,
                'streaming': bool(kwargs.get('streaming', False)),
                'path': str(kwargs.get('path', '') or ''),
            })
            if now - self._last_persist >= self._persist_interval:
                self._last_persist = now
                self._persist()

    async def record_tokens(self, model: str = "", prompt_tokens: int = 0,
                            completion_tokens: int = 0, key_label: str = "",
                            latency_ms: float | None = None):
        """Add usage tokens without re-counting the request (DASH fix).

        record_request already counted the request when the upstream response
        started; tokens for streaming responses only become known at
        end-of-stream, so they are added here."""
        if prompt_tokens <= 0 and completion_tokens <= 0 and latency_ms is None:
            return
        now = time.time()
        try:
            latency_value = max(0.0, float(latency_ms)) if latency_ms is not None else None
        except (TypeError, ValueError):
            latency_value = None
        with self._lock:
            self.tokens_in += prompt_tokens
            self.tokens_out += completion_tokens
            if model:
                m = self.per_model.setdefault(model, {
                    'requests': 0, 'errors': 0, 'success_count': 0,
                    'tokens_in': 0, 'tokens_out': 0,
                    'latency_total_ms': 0.0, 'latency_count': 0,
                    'last_latency_ms': None, 'max_latency_ms': 0.0,
                    'first_ts': now, 'last_ts': now, 'last_status': 200,
                })
                m['tokens_in'] += prompt_tokens
                m['tokens_out'] += completion_tokens
                if latency_value is not None:
                    m['latency_total_ms'] = m.get('latency_total_ms', 0.0) + latency_value
                    m['latency_count'] = m.get('latency_count', 0) + 1
                    m['last_latency_ms'] = latency_value
                    m['max_latency_ms'] = max(m.get('max_latency_ms', 0.0), latency_value)
                m['last_ts'] = now
            if key_label:
                k = self.per_key.setdefault(key_label, {
                    'requests': 0, 'errors': 0, 'rate_limited': 0,
                    'tokens_in': 0, 'tokens_out': 0, 'last_used_ts': now,
                })
                k['tokens_in'] += prompt_tokens
                k['tokens_out'] += completion_tokens
            hour = int(now // 3600 * 3600)
            h = self.hourly.setdefault(hour, {
                'requests': 0, 'errors': 0, 'rate_limited': 0, 'tokens': 0,
            })
            h['tokens'] += prompt_tokens + completion_tokens
            wanted_model = model or 'unknown'
            wanted_key = key_label or '—'
            for row in reversed(self.activity):
                if (row.get('model') == wanted_model
                        and row.get('key_label', '—') == wanted_key
                        and (row.get('prompt_tokens', 0) == 0
                             or row.get('latency_ms') is None)):
                    if prompt_tokens > 0:
                        row['prompt_tokens'] = int(prompt_tokens)
                    if completion_tokens > 0:
                        row['completion_tokens'] = int(completion_tokens)
                    if latency_value is not None:
                        row['latency_ms'] = latency_value
                    break
            if now - self._last_persist >= self._persist_interval:
                self._last_persist = now
                self._persist()

    def record_error(self, status_code: int = 0, **kwargs):
        with self._lock:
            self.errors += 1
            self.requests += 1

    async def summary(self, window: str = "24h") -> dict:
        uptime = time.time() - self.start
        with self._lock:
            return {
                "uptime_seconds": int(uptime),
                "total_requests": self.requests,
                "total_tokens": self.tokens_in + self.tokens_out,
                "input_tokens": self.tokens_in,
                "output_tokens": self.tokens_out,
                "error_rate": min(1.0, round(self.errors / max(1, self.requests), 4)),
            }

    def per_model_summary(self) -> list:
        with self._lock:
            rows = []
            for model, m in self.per_model.items():
                reqs = m.get('requests', 0)
                succ = m.get('success_count', 0)
                rows.append({
                    'model': model, 'requests': reqs,
                    'error_count': m.get('errors', 0), 'success_count': succ,
                    'success_rate': round(100.0 * succ / reqs, 1) if reqs else 100.0,
                    'prompt_tokens': m.get('tokens_in', 0),
                    'completion_tokens': m.get('tokens_out', 0),
                    'total_tokens': m.get('tokens_in', 0) + m.get('tokens_out', 0),
                    'avg_latency_ms': (m.get('latency_total_ms', 0.0) / m.get('latency_count', 1)
                                       if m.get('latency_count', 0) else None),
                    'max_latency_ms': m.get('max_latency_ms'),
                    'last_latency_ms': m.get('last_latency_ms'),
                    'first_ts': m.get('first_ts'), 'last_ts': m.get('last_ts'),
                    'last_status': m.get('last_status', 200),
                })
        rows.sort(key=lambda r: (r.get('last_ts') or 0), reverse=True)
        return rows

    def recent_activity(self, limit: int = 50, offset: int = 0) -> list:
        with self._lock:
            rows = list(self.activity)
        rows.reverse()
        start = max(0, int(offset))
        end = start + max(1, int(limit))
        return rows[start:end]

    def per_key_summary(self) -> list:
        now = time.time()
        with self._lock:
            rows = []
            for label, k in self.per_key.items():
                last_ts = k.get('last_used_ts')
                rows.append({
                    'key_label': label, 'requests': k.get('requests', 0),
                    'rate_limited_count': k.get('rate_limited', 0),
                    'errors': k.get('errors', 0),
                    'prompt_tokens': k.get('tokens_in', 0),
                    'completion_tokens': k.get('tokens_out', 0),
                    'total_tokens': k.get('tokens_in', 0) + k.get('tokens_out', 0),
                    'last_used_ago_s': int(now - last_ts) if last_ts else None,
                })
        rows.sort(key=lambda row: row.get('requests', 0), reverse=True)
        return rows

    def hourly_chart(self, hours: int = 24) -> list:
        hours = max(1, min(168, int(hours)))
        now = int(time.time())
        with self._lock:
            out = []
            for ago in range(hours - 1, -1, -1):
                bucket = int((now - ago * 3600) // 3600 * 3600)
                item = self.hourly.get(bucket, {})
                out.append({
                    'ts': bucket, 'hour_ago': ago,
                    'requests': item.get('requests', 0),
                    'tokens': item.get('tokens', 0),
                    'total_tokens': item.get('tokens', 0),
                    'rate_limited': item.get('rate_limited', 0),
                    'errors': item.get('errors', 0),
                })
            return out

    async def close(self):
        self._persist()

    def prom_metrics(self) -> str:
        with self._lock:
            return f"""# HELP openrouter_requests_total Total requests
# TYPE openrouter_requests_total counter
openrouter_requests_total {self.requests}
# HELP openrouter_tokens_total Total tokens
# TYPE openrouter_tokens_total counter
openrouter_tokens_total {self.tokens_in + self.tokens_out}
# HELP openrouter_errors_total Total errors
# TYPE openrouter_errors_total counter
openrouter_errors_total {self.errors}
"""
