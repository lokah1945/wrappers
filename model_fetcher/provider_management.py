#!/usr/bin/env python3
"""
provider_management.py — OpenRouter Management API integration.

Provides programmatic CRUD for OpenRouter API keys using the Management API.
Only for admin operations (list, create, rotate, disable keys).
Management keys CANNOT be used for inference.
"""

import os
import json
import logging
from dataclasses import dataclass
from typing import Optional
from pathlib import Path

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

logger = logging.getLogger("provider-management")


@dataclass
class ManagementResult:
    success: bool
    data: any = None
    error: str = None


class ProviderManager:
    """OpenRouter Management API client."""
    
    def __init__(self):
        self.management_key = os.environ.get("OPENROUTER_MANAGEMENT_KEY", "").strip()
        self.base_url = "https://openrouter.ai/api/v1"
        self._enabled = bool(self.management_key)
    
    def is_enabled(self, provider: str = "openrouter") -> bool:
        """Check if management is configured for provider."""
        if provider != "openrouter":
            return False
        return self._enabled
    
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.management_key}",
            "Content-Type": "application/json",
        }
    
    async def _request(self, method: str, path: str, **kwargs) -> ManagementResult:
        if not self._enabled:
            return ManagementResult(False, error="Management API not configured (set OPENROUTER_MANAGEMENT_KEY)")
        
        if not HAS_AIOHTTP:
            return ManagementResult(False, error="aiohttp not installed")
        
        url = f"{self.base_url}{path}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=self._headers(), **kwargs) as resp:
                    data = await resp.json()
                    if resp.status >= 400:
                        return ManagementResult(False, error=data.get("error", {}).get("message", f"HTTP {resp.status}"))
                    return ManagementResult(True, data=data)
        except Exception as e:
            return ManagementResult(False, error=str(e))
    
    # === Key Management ===
    
    async def list_keys(self, offset: int = 0, limit: int = 100) -> ManagementResult:
        """List API keys."""
        return await self._request("GET", f"/keys?offset={offset}&limit={limit}")
    
    async def get_key(self, key_hash: str) -> ManagementResult:
        """Get key details."""
        return await self._request("GET", f"/keys/{key_hash}")
    
    async def create_key(
        self,
        name: str,
        limit: Optional[float] = None,
        rate_limit: Optional[int] = None,
        models: Optional[list] = None,
    ) -> ManagementResult:
        """Create a new API key."""
        payload = {"name": name}
        if limit is not None:
            payload["limit"] = limit
        if rate_limit is not None:
            payload["rate_limit"] = rate_limit
        if models:
            payload["models"] = models
        return await self._request("POST", "/keys", json=payload)
    
    async def update_key(
        self,
        key_hash: str,
        name: Optional[str] = None,
        disabled: Optional[bool] = None,
        limit: Optional[float] = None,
        limit_reset_at: Optional[str] = None,
    ) -> ManagementResult:
        """Update an existing key."""
        payload = {}
        if name is not None:
            payload["name"] = name
        if disabled is not None:
            payload["disabled"] = disabled
        if limit is not None:
            payload["limit"] = limit
        if limit_reset_at is not None:
            payload["limit_reset_at"] = limit_reset_at
        return await self._request("PATCH", f"/keys/{key_hash}", json=payload)
    
    async def delete_key(self, key_hash: str) -> ManagementResult:
        """Permanently delete a key."""
        return await self._request("DELETE", f"/keys/{key_hash}")
    
    async def rotate_key(self, key_hash: str) -> ManagementResult:
        """Zero-downtime key rotation (create new, disable old)."""
        # Get old key info first
        old = await self.get_key(key_hash)
        if not old.success:
            return old
        
        old_data = old.data.get("data", {})
        name = f"{old_data.get('name', 'key')}-rotated"
        
        # Create new key
        new = await self.create_key(
            name=name,
            limit=old_data.get("limit"),
            rate_limit=old_data.get("rate_limit"),
            models=old_data.get("models"),
        )
        if not new.success:
            return new
        
        # Disable old key
        await self.update_key(key_hash, disabled=True)
        
        return new
    
    async def get_usage(self, key_hash: Optional[str] = None, days: int = 30) -> ManagementResult:
        """Get usage stats for keys."""
        path = f"/keys/usage?days={days}"
        if key_hash:
            path += f"&key={key_hash}"
        return await self._request("GET", path)


# Global instance
MGT = ProviderManager()


def is_management_enabled(provider: str = "openrouter") -> bool:
    return MGT.is_enabled(provider)


async def openrouter_list_keys(offset: int = 0) -> ManagementResult:
    return await MGT.list_keys(offset=offset)


async def openrouter_get_key(key_hash: str) -> ManagementResult:
    return await MGT.get_key(key_hash)


async def openrouter_create_key(
    name: str,
    limit: Optional[float] = None,
    rate_limit: Optional[int] = None,
    models: Optional[list] = None,
) -> ManagementResult:
    return await MGT.create_key(name, limit, rate_limit, models)


async def openrouter_update_key(
    key_hash: str,
    name: Optional[str] = None,
    disabled: Optional[bool] = None,
    limit: Optional[float] = None,
    limit_reset_at: Optional[str] = None,
) -> ManagementResult:
    return await MGT.update_key(key_hash, name, disabled, limit, limit_reset_at)


async def openrouter_delete_key(key_hash: str) -> ManagementResult:
    return await MGT.delete_key(key_hash)


async def openrouter_rotate_key(key_hash: str) -> ManagementResult:
    return await MGT.rotate_key(key_hash)


async def openrouter_key_usage(key_hash: Optional[str] = None, days: int = 30) -> ManagementResult:
    return await MGT.get_usage(key_hash, days)


async def get_provider_keys_status() -> dict:
    """Get status of all provider management configs."""
    return {
        "openrouter": MGT.is_enabled("openrouter"),
    }


if __name__ == "__main__":
    import asyncio
    
    async def test():
        if not MGT.is_enabled():
            print("Management not enabled - set OPENROUTER_MANAGEMENT_KEY")
            return
        
        result = await MGT.list_keys()
        print(json.dumps(result.data, indent=2))
    
    asyncio.run(test())