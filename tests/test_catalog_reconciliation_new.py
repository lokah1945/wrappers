from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from common.model import LocalModelRegistry
from common.model_catalog import CachedModelCatalog
from common.model_state import ModelStateStore


async def _cache(tmp_path: Path):
    store = ModelStateStore("provider", tmp_path / "state.db", catalog_ttl_sec=3600)
    registry = LocalModelRegistry("provider", tmp_path / "profiles", tmp_path / "profiles.db")
    cache = CachedModelCatalog(
        store, registry, SimpleNamespace(schedule_catalog=lambda *args: None),
        "https://provider.test/v1/models", ttl_sec=3600,
    )
    return cache, store


@pytest.mark.asyncio
async def test_successful_full_refresh_retires_absent_rows(tmp_path: Path):
    cache, store = await _cache(tmp_path)
    assert [m["id"] for m in await cache.refresh(
        lambda: asyncio.sleep(0, result=[{"id": "vendor/a"}, {"id": "vendor/b"}]),
        force=True,
    )] == ["vendor/a", "vendor/b"]
    await cache.refresh(lambda: asyncio.sleep(0, result=[{"id": "vendor/a"}]), force=True)
    assert store.get_ids() == ["vendor/a"]
    assert store.catalog_status()["state"] == "fresh"
    await cache.close()


@pytest.mark.asyncio
async def test_failed_refresh_retains_last_good_and_exposes_error(tmp_path: Path):
    cache, store = await _cache(tmp_path)
    await cache.refresh(lambda: asyncio.sleep(0, result=[{"id": "vendor/a"}]), force=True)

    async def fail():
        raise OSError("controlled outage")

    await cache.refresh(fail, force=True)
    assert store.get_ids() == ["vendor/a"]
    status = cache.status()
    assert status["state"] == "stale"
    assert status["stale"] is True
    assert status["last_error"]["code"] == "OSError"
    await cache.close()


def test_catalog_envelope_round_trips_without_credentials(tmp_path: Path):
    store = ModelStateStore("provider", tmp_path / "state.db")
    envelope = {"data": [{"id": "vendor/a", "future": {"opaque": True}}],
                "links": {"next": "https://provider.test/v1/models?offset=1"},
                "total_count": 2}
    store.save_catalog_envelope(envelope, source="provider")
    assert store.get_catalog_envelope() == envelope
