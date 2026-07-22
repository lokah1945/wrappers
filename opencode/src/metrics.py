#!/usr/bin/env python3
"""Simple metrics for wrapper-opencode."""

import time
from typing import Dict

class Metrics:
    def __init__(self, db_path: str = None):
        self.start = time.time()
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0

    async def init(self):
        pass

    async def record_request(self, model: str = "", prompt_tokens: int = 0, completion_tokens: int = 0, **kwargs):
        self.requests += 1
        self.tokens_in += prompt_tokens
        self.tokens_out += completion_tokens

    async def summary(self, window: str = "24h") -> Dict:
        uptime = time.time() - self.start
        return {
            "uptime_seconds": int(uptime),
            "total_requests": self.requests,
            "total_tokens": self.tokens_in + self.tokens_out,
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            "error_rate": round(self.errors / max(1, self.requests), 4),
        }

    async def close(self):
        pass

    def prom_metrics(self) -> str:
        return f"""# HELP opencode_requests_total Total requests
# TYPE opencode_requests_total counter
opencode_requests_total {self.requests}
"""