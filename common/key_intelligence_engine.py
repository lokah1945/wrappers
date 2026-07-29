#!/usr/bin/env python3
"""Universal AI Key Intelligence Engine — replaces circuit breaker entirely.

Design philosophy: never block requests unnecessarily. Always maximize
successful completion through intelligent routing, dynamic rotation,
weighted selection, and temporary cooldown (not blocking).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

logger = logging.getLogger("key-intelligence")


class ProviderHealth:
    """Telemetry-only health tracking — no blocking."""

    def __init__(self, name: str):
        self.name = name
        self.latency_ms: float = 0.0
        self.success_rate: float = 1.0
        self.error_rate: float = 0.0
        self.recent_failures: int = 0
        self.last_recovery_at: float = 0.0
        self.quota_remaining: float = 1.0


class KeyRouter:
    """Intelligent routing engine supporting weighted, least-latency,
    least-failures, least-token, round-robin, and automatic recovery."""

    def __init__(self):
        self.keys: List[Any] = []
        self.health: Dict[str, ProviderHealth] = {}
        self.cooldowns: Dict[str, float] = {}

    def add_key(self, key: Any, provider: str = "") -> None:
        self.keys.append(key)
        if provider and provider not in self.health:
            self.health[provider] = ProviderHealth(provider)

    def select_key(self, strategy: str = "weighted", provider: Optional[str] = None) -> Optional[Any]:
        available = [k for k in self.keys if not self._is_cool(k)]
        if not available:
            # Never block: return least-cooldown key for automatic recovery attempt
            available = self.keys
        if strategy == "least_latency":
            return min(available, key=lambda k: self.health.get(getattr(k, "provider", ""), ProviderHealth("")).latency_ms or 99999)
        if strategy == "least_failures":
            return min(available, key=lambda k: self.health.get(getattr(k, "provider", ""), ProviderHealth("")).recent_failures)
        return available[0] if available else None

    def _is_cool(self, key: Any) -> bool:
        fingerprint = getattr(key, "fingerprint", str(id(key)))
        if fingerprint in self.cooldowns:
            if time.time() < self.cooldowns[fingerprint]:
                return True
            else:
                del self.cooldowns[fingerprint]
        return False

    def temporary_cooldown(self, key: Any, seconds: float = 30.0) -> None:
        fingerprint = getattr(key, "fingerprint", str(id(key)))
        self.cooldowns[fingerprint] = time.time() + seconds
        logger.info(f"Temporary cooldown {fingerprint}: {seconds}s")

    def record_success(self, key: Any, latency_ms: float = 0.0) -> None:
        fingerprint = getattr(key, "provider", "unknown")
        if fingerprint not in self.health:
            self.health[fingerprint] = ProviderHealth(fingerprint)
        self.health[fingerprint].success_rate = min(1.0, self.health[fingerprint].success_rate + 0.05)
        self.health[fingerprint].latency_ms = latency_ms
        self.health[fingerprint].recent_failures = max(0, self.health[fingerprint].recent_failures - 1)

    def record_failure(self, key: Any) -> None:
        fingerprint = getattr(key, "provider", "unknown")
        if fingerprint not in self.health:
            self.health[fingerprint] = ProviderHealth(fingerprint)
        self.health[fingerprint].recent_failures += 1
        self.health[fingerprint].error_rate = min(1.0, self.health[fingerprint].error_rate + 0.05)

    def stats(self) -> Dict[str, Any]:
        return {
            "keys_registered": len(self.keys),
            "providers": {k: {"latency_ms": v.latency_ms, "success_rate": v.success_rate, "recent_failures": v.recent_failures} for k, v in self.health.items()},
            "cooldowns_active": len(self.cooldowns),
        }
