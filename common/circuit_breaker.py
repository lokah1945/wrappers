#!/usr/bin/env python3
"""Circuit breaker pattern for upstream API calls.

Prevents cascading failures when an upstream provider is experiencing issues.
After a configurable number of consecutive failures, the circuit opens and
requests are short-circuited for a cooldown period.

States:
  CLOSED   - Normal operation, requests pass through
  OPEN     - Failures exceeded threshold, requests are rejected immediately
  HALF_OPEN - After cooldown, one probe request is allowed through

Usage:
    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

    async def call_upstream():
        async with breaker:
            return await make_request()
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import Optional

logger = logging.getLogger('circuit-breaker')


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    """Raised when the circuit is open and requests are rejected."""

    def __init__(self, state: CircuitState, remaining_seconds: float = 0):
        self.state = state
        self.remaining_seconds = remaining_seconds
        msg = f"Circuit breaker is {state.value}"
        if remaining_seconds > 0:
            msg += f" ({remaining_seconds:.0f}s remaining)"
        super().__init__(msg)


class CircuitBreaker:
    """Async circuit breaker for upstream API protection.

    Args:
        failure_threshold: Consecutive failures before opening the circuit.
        recovery_timeout: Seconds to wait before transitioning to half-open.
        success_threshold: Consecutive successes in half-open before closing.
        name: Identifier for logging (e.g., 'nvidia-upstream').
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
        name: str = "default",
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        self.name = name

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state, accounting for recovery timeout."""
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    @property
    def remaining_recovery_seconds(self) -> float:
        """Seconds remaining before half-open transition."""
        if self._state != CircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._last_failure_time
        return max(0.0, self.recovery_timeout - elapsed)

    async def __aenter__(self):
        await self.before_request()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self.record_failure()
        else:
            await self.record_success()
        return False

    async def before_request(self) -> None:
        """Check if the request is allowed through the circuit."""
        async with self._lock:
            # BUG-ECB1 fix: transition _state to HALF_OPEN when timeout elapses.
            # Previously `state` property returned HALF_OPEN without updating
            # `_state`, causing record_success/failure to never see HALF_OPEN.
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info(f"[circuit-breaker:{self.name}] transitioning to half-open")

            current = self._state
            if current == CircuitState.CLOSED:
                return
            elif current == CircuitState.HALF_OPEN:
                logger.info(f"[circuit-breaker:{self.name}] half-open: allowing probe request")
                return
            else:  # OPEN
                remaining = self.remaining_recovery_seconds
                logger.warning(
                    f"[circuit-breaker:{self.name}] circuit open, rejecting request "
                    f"({remaining:.0f}s remaining)"
                )
                raise CircuitBreakerError(CircuitState.OPEN, remaining)

    async def record_success(self) -> None:
        """Record a successful request."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    logger.info(
                        f"[circuit-breaker:{self.name}] closing circuit after "
                        f"{self._success_count} successes"
                    )
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0

    async def record_failure(self) -> None:
        """Record a failed request."""
        async with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self._success_count = 0

            if self._state == CircuitState.HALF_OPEN:
                logger.warning(
                    f"[circuit-breaker:{self.name}] probe failed, reopening circuit"
                )
                self._state = CircuitState.OPEN
            elif self._failure_count >= self.failure_threshold:
                logger.warning(
                    f"[circuit-breaker:{self.name}] opening circuit after "
                    f"{self._failure_count} consecutive failures"
                )
                self._state = CircuitState.OPEN

    def stats(self) -> dict:
        """Return circuit breaker statistics."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "failure_threshold": self.failure_threshold,
            "recovery_timeout": self.recovery_timeout,
            "remaining_recovery_seconds": round(self.remaining_recovery_seconds, 1),
        }

    async def reset(self) -> None:
        """Manually reset the circuit to closed state."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            logger.info(f"[circuit-breaker:{self.name}] manually reset to closed")
