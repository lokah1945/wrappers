#!/usr/bin/env python3
"""Metrics collection for Nous Research API wrapper."""

import time

class Metrics:
    def __init__(self):
        self.requests = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.errors = 0
        self.start = time.time()

    def record(self, prompt=0, completion=0, error=False):
        self.requests += 1
        self.tokens_in += prompt
        self.tokens_out += completion
        if error: self.errors += 1

    def snapshot(self):
        uptime = time.time() - self.start
        return {
            "uptime_seconds": int(uptime),
            "total_requests": self.requests,
            "total_tokens": self.tokens_in + self.tokens_out,
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            "error_rate": round(self.errors / max(1, self.requests), 4)
        }

metrics = Metrics()

metrics = Metrics()
