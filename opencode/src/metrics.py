#!/usr/bin/env python3
"""Persistent metrics for wrapper-opencode.

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


class Metrics:
    def __init__(self, db_path: str = None):
        self.start = time.time()
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self._db_path = db_path
        self._lock = threading.Lock()
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
            self.tokens_in += prompt_tokens
            self.tokens_out += completion_tokens
            if kwargs.get('status_code', 200) >= 400:
                self.errors += 1

    def record_error(self, status_code: int = 0, **kwargs):
        # OC-14: synchronous error counter so _jr (and other sync call sites) can
        # record failures without awaiting on the request coroutine.
        with self._lock:
            self.errors += 1

    async def summary(self, window: str = "24h") -> Dict:
        uptime = time.time() - self.start
        with self._lock:
            return {
                "uptime_seconds": int(uptime),
                "total_requests": self.requests,
                "total_tokens": self.tokens_in + self.tokens_out,
                "input_tokens": self.tokens_in,
                "output_tokens": self.tokens_out,
                "error_rate": round(self.errors / max(1, self.requests), 4),
            }

    async def close(self):
        self._persist()

    def prom_metrics(self) -> str:
        with self._lock:
            return f"""# HELP opencode_requests_total Total requests
# TYPE opencode_requests_total counter
opencode_requests_total {self.requests}
# HELP opencode_tokens_total Total tokens
# TYPE opencode_tokens_total counter
opencode_tokens_total {self.tokens_in + self.tokens_out}
# HELP opencode_errors_total Total errors
# TYPE opencode_errors_total counter
opencode_errors_total {self.errors}
"""
