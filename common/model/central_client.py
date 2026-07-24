"""Optional async client for the central model intelligence service.

The client is deliberately best-effort. A registry outage must never change,
block, or substitute an inference request; wrappers continue using local
profiles/cache.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import suppress
from typing import Any

import aiohttp

from .sanitize import sanitize_error_detail


class ModelRegistryClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout_sec: float = 3.0):
        self.base_url = (base_url or os.environ.get("MODEL_REGISTRY_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("MODEL_REGISTRY_ADMIN_TOKEN", "")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)
        self.queue_limit = int(os.environ.get("MODEL_REGISTRY_OBSERVATION_QUEUE", "1000"))
        self._session: aiohttp.ClientSession | None = None
        self._session_lock: asyncio.Lock | None = None
        self._worker: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=self.queue_limit)
        self.dropped_observations = 0
        self.failed_posts = 0
        self.consecutive_failures = 0
        self.circuit_open_until = 0.0
        self.circuit_threshold = int(os.environ.get("MODEL_REGISTRY_CIRCUIT_THRESHOLD", "3"))
        self.circuit_cooldown_sec = int(os.environ.get("MODEL_REGISTRY_CIRCUIT_COOLDOWN_SEC", "30"))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _ensure_session(self) -> aiohttp.ClientSession | None:
        if not self.enabled:
            return None
        loop = asyncio.get_running_loop()
        if self._session is not None:
            owner_loop = getattr(self._session, "_loop", None)
            if not self._session.closed and owner_loop is loop:
                return self._session
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def start(self) -> None:
        if not self.enabled:
            return
        await self._ensure_session()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._observation_worker())

    async def stop(self) -> None:
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._start_task
        self._start_task = None
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _post(self, path: str, payload: dict[str, Any]) -> bool:
        if time.time() < self.circuit_open_until:
            return False
        session = await self._ensure_session()
        if session is None:
            return False
        try:
            async with session.post(
                f"{self.base_url}{path}", json=payload, headers=self._headers()
            ) as response:
                ok = 200 <= response.status < 300
                if ok:
                    self.consecutive_failures = 0
                    self.circuit_open_until = 0.0
                else:
                    self.failed_posts += 1
                    self.consecutive_failures += 1
                if self.consecutive_failures >= self.circuit_threshold:
                    self.circuit_open_until = time.time() + self.circuit_cooldown_sec
                return ok
        except Exception:
            self.failed_posts += 1
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.circuit_threshold:
                self.circuit_open_until = time.time() + self.circuit_cooldown_sec
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "queue_depth": self._queue.qsize(),
            "queue_limit": self.queue_limit,
            "dropped_observations": self.dropped_observations,
            "failed_posts": self.failed_posts,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": time.time() < self.circuit_open_until,
            "worker_running": bool(self._worker and not self._worker.done()),
        }

    async def _observation_worker(self) -> None:
        while True:
            path, payload = await self._queue.get()
            try:
                await self._post(path, payload)
            finally:
                self._queue.task_done()

    async def ingest_catalog(self, provider: str, models: list[Any], revision: str) -> bool:
        return await self._post(
            "/internal/catalog",
            {"provider": provider, "models": models, "revision": revision},
        )

    def schedule_catalog(self, provider: str, models: list[Any], revision: str) -> None:
        """Queue catalog sync so discovery responses do not wait on the control plane."""
        self._schedule(
            "/internal/catalog",
            {"provider": provider, "models": models, "revision": revision},
        )

    @staticmethod
    def canonical_model_id(provider: str, model_id: str) -> str:
        prefix = f"{provider}/"
        return model_id if model_id.startswith(prefix) else prefix + model_id

    async def observe(self, provider: str, canonical_model_id: str,
                      account_scope_hash: str, state: str, status: int,
                      reason_code: str, reason_detail: str, endpoint: str) -> bool:
        return await self._post(
            "/internal/observations",
            {
                "provider": provider,
                "canonical_model_id": canonical_model_id,
                "account_scope_hash": account_scope_hash,
                "state": state,
                "http_status": status,
                "reason_code": reason_code,
                "reason_detail": sanitize_error_detail(reason_detail),
                "endpoint": endpoint,
            },
        )

    def _schedule(self, path: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.dropped_observations += 1
            return
        try:
            self._queue.put_nowait((path, payload))
        except asyncio.QueueFull:
            self.dropped_observations += 1
            return
        if ((self._worker is None or self._worker.done()) and
                (self._start_task is None or self._start_task.done())):
            self._start_task = loop.create_task(self.start())

    def schedule_observation(self, provider: str, model_id: str,
                             account_scope_hash: str, state: str, status: int,
                             reason_code: str, reason_detail: str,
                             endpoint: str) -> None:
        """Queue a bounded non-blocking observation; never affects inference."""
        payload = {
            "provider": provider,
            "canonical_model_id": self.canonical_model_id(provider, model_id),
            "account_scope_hash": account_scope_hash,
            "state": state,
            "http_status": status,
            "reason_code": reason_code,
            "reason_detail": sanitize_error_detail(reason_detail),
            "endpoint": endpoint,
        }
        self._schedule("/internal/observations", payload)
