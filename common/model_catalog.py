"""Persistent catalog with single-flight refresh and stale-while-revalidate.

Only discovery/background refresh calls fetch; inference never consults the
upstream for metadata. Failed refreshes are throttled and retain last-good data.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from contextlib import suppress

from common.model.metadata import annotate_catalog

logger = logging.getLogger(__name__)


class CachedModelCatalog:
    def __init__(self, store, registry, client, source, *, ttl_sec=21600):
        self.store, self.registry, self.client = store, registry, client
        self.source = source
        self.ttl_sec = max(60, ttl_sec)
        self._models = None
        self._last_attempt = None
        self._lock = asyncio.Lock()
        self._task = None
        self._generation = 0

    def snapshot(self):
        if self._models is None:
            self._models = [entry for entry in self.store.get_catalog(fresh_only=False)
                            if entry.get('context_metadata', {}).get('source') == self.source]
            if self._models:
                self.registry.register_catalog(self._models, revision='cached-catalog')
        return copy.deepcopy(self._models)

    async def refresh(self, fetch, *, force=False):
        started = time.monotonic()
        generation = self._generation
        async with self._lock:
            # Concurrent force refreshes also coalesce into the first fetch.
            if generation != self._generation or (self._last_attempt is not None and (
                self._last_attempt >= started or
                (not force and started - self._last_attempt < min(60, self.ttl_sec))
            )):
                return self.snapshot()
            self._last_attempt = time.monotonic()
            try:
                entries = await fetch()
                if not isinstance(entries, list) or not entries:
                    raise ValueError('catalog contains no models')
                now = time.time()
                models = [annotate_catalog(m, self.source, checked_at=now, ttl_sec=self.ttl_sec)
                          for m in entries if isinstance(m, dict) and m.get('id')]
                if not models:
                    raise ValueError('catalog contains no valid model records')
                self.store.upsert_catalog(models, source=self.source, ttl_sec=self.ttl_sec)
                self.registry.register_catalog(models, revision='runtime-catalog')
                self._models = models
                self.client.schedule_catalog(self.store.provider, models, 'runtime-catalog')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Catalog refresh failed (%s): %s', self.store.provider, type(exc).__name__)
            finally:
                self._generation += 1
            return self.snapshot()

    async def get(self, fetch):
        models = self.snapshot()
        if not models:
            return await self.refresh(fetch)
        age = self.store.catalog_age_sec()
        if age is None or age >= self.ttl_sec:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self.refresh(fetch))
        return models

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
