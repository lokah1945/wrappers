"""Optional async client for the central model intelligence service.

The client is deliberately best-effort. A registry outage must never change,
block, or substitute an inference request; wrappers continue using local
profiles/cache.
"""

from __future__ import annotations

import os
from typing import Any

import aiohttp


class ModelRegistryClient:
    def __init__(self, base_url: str | None = None, token: str | None = None,
                 timeout_sec: float = 3.0):
        self.base_url = (base_url or os.environ.get("MODEL_REGISTRY_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("MODEL_REGISTRY_ADMIN_TOKEN", "")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.post(
                    f"{self.base_url}{path}", json=payload, headers=self._headers()
                ) as response:
                    return 200 <= response.status < 300
        except Exception:
            return False

    async def ingest_catalog(self, provider: str, models: list[Any], revision: str) -> bool:
        return await self._post(
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
                "reason_detail": str(reason_detail or "")[:4000],
                "endpoint": endpoint,
            },
        )

    def schedule_observation(self, provider: str, model_id: str,
                             account_scope_hash: str, state: str, status: int,
                             reason_code: str, reason_detail: str,
                             endpoint: str) -> None:
        """Best-effort non-blocking observation; never affects inference."""
        if not self.enabled:
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.observe(
                provider,
                self.canonical_model_id(provider, model_id),
                account_scope_hash,
                state,
                status,
                reason_code,
                reason_detail,
                endpoint,
            ))
        except RuntimeError:
            return
