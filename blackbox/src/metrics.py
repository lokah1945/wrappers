#!/usr/bin/env python3
"""Persistent metrics for wrapper-blackbox.

Upgraded from in-memory-only counters to support JSON-file persistence
on shutdown/restart, matching the reliability guarantees of nvidia's
SQLite-backed metrics while keeping the dependency footprint minimal.
"""

import json
import os
import time
import threading
from pathlib import Path
from typing import Dict


def _finite_nonneg_int(value):
    """B-27.1: clamp token counters to finite non-negative ints — a NaN/Inf
    literal from an upstream usage object (Python's json.loads accepts them)
    would otherwise poison the counter FOREVER (NaN is sticky) and leak into
    persisted snapshots, making every future /metrics payload invalid JSON.
    Twin of common.translations.shared.finite_nonneg_int."""
    try:
        v = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return v if v > 0 else 0



class Metrics:
    def __init__(self, db_path: str = None):
        self.start = time.time()
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self._db_path = db_path
        self._lock = threading.Lock()
        # BB-15/OC-14: persist periodically, not only on graceful shutdown,
        # so counters survive SIGKILL/OOM.
        self._persist_interval = float(os.environ.get('METRICS_PERSIST_SEC', '60'))
        self._last_persist = time.time()
        self._load_persisted()

    def _persist_path(self) -> str:
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
                    'saved_at': time.time(),
                }, f)
        except Exception:
            pass

    async def init(self):
        pass

    async def record_request(self, model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0, **kwargs):
        with self._lock:
            self.requests += 1
            self.tokens_in += _finite_nonneg_int(prompt_tokens)
            self.tokens_out += _finite_nonneg_int(completion_tokens)
            if kwargs.get('status_code', 200) >= 400:
                self.errors += 1
            # BB-15/OC-14: periodic persistence (cheap small-file JSON dump).
            now = time.time()
            if now - self._last_persist >= self._persist_interval:
                self._last_persist = now
                self._persist()

    def record_error(self, status_code: int = 0, **kwargs):
        """B-39 fix: parity with opencode/openrouter.

        blackbox was the only wrapper whose Metrics class lacked record_error,
        so error paths that did not also call record_request could never move
        the errors counter.
        """
        with self._lock:
            self.errors += 1
            self.requests += 1
            now = time.time()
            if now - self._last_persist >= self._persist_interval:
                self._last_persist = now
                self._persist()

    async def summary(self, window: str = "24h") -> Dict:
        uptime = time.time() - self.start
        with self._lock:
            if self.requests == 0:
                error_rate = 0.0
            else:
                error_rate = round(self.errors / self.requests, 4)
            return {
                "uptime_seconds": int(uptime),
                "total_requests": self.requests,
                "total_tokens": self.tokens_in + self.tokens_out,
                "input_tokens": self.tokens_in,
                "output_tokens": self.tokens_out,
                # B-39 visibility (2026-08-03 R4): absolute count, not just the rate.
                "total_errors": self.errors,
                "error_rate": error_rate,
            }

    async def close(self):
        self._persist()

    def prom_metrics(self) -> str:
        with self._lock:
            return f"""# HELP blackbox_requests_total Total requests
# TYPE blackbox_requests_total counter
blackbox_requests_total {self.requests}
# HELP blackbox_tokens_total Total tokens
# TYPE blackbox_tokens_total counter
blackbox_tokens_total {self.tokens_in + self.tokens_out}
# HELP blackbox_errors_total Total errors
# TYPE blackbox_errors_total counter
blackbox_errors_total {self.errors}
"""
