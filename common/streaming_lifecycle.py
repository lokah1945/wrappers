#!/usr/bin/env python3
"""Universal Streaming Lifecycle Validator — evidence-backed production module."""
from __future__ import annotations
import asyncio, time, logging
logger = logging.getLogger("streaming-lifecycle")

class StreamingLifecycle:
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.events = []
        self.terminated = False
        self.start_time = time.time()

    async def emit(self, event: dict) -> None:
        if self.terminated:
            raise RuntimeError(f"[{self.request_id}] Streaming terminated; cannot emit.")
        event["ts"] = time.time()
        self.events.append(event)
        logger.info(f"[{self.request_id}] event={event.get('type')} at {event['ts']}")

    async def terminate(self, reason: str = "complete") -> None:
        if self.terminated:
            return
        self.terminated = True
        self.events.append({"type": "terminate", "reason": reason, "duration_sec": time.time() - self.start_time})
        logger.info(f"[{self.request_id}] terminated: {reason} duration={self.events[-1]['duration_sec']:.3f}s events={len(self.events)}")

    def is_complete(self) -> bool:
        return self.terminated and any(e.get("type") == "terminate" for e in self.events)
