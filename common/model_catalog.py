"""Persistent provider catalog with single-flight refresh and SWR semantics.

A successful full provider snapshot retires rows absent from that snapshot. A
failed refresh never destroys the last-good rows; callers can expose the
persisted freshness/error state through ``status()``.
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
        self._refreshing = False
        self._last_error = None

    def snapshot(self):
        if self._models is None:
            # Only load rows carrying this exact provider-source provenance.
            # Curated/legacy rows without provenance must not become public
            # native catalog data after a restart. A successful refresh writes
            # the current source and reconciles removed IDs.
            self._models = [entry for entry in self.store.get_catalog(fresh_only=False)
                            if isinstance(entry, dict) and entry.get('id')
                            and entry.get('context_metadata', {}).get('source') == self.source]
            if self._models:
                self.registry.register_catalog(self._models, revision='cached-catalog')
        return copy.deepcopy(self._models or [])

    def status(self):
        status_fn = getattr(self.store, 'catalog_status', None)
        if callable(status_fn):
            try:
                status = dict(status_fn())
            except Exception:
                status = {'state': 'unknown', 'stale': True}
        else:
            status = {'state': 'unknown', 'stale': True}
        status['refreshing'] = bool(self._refreshing)
        if self._last_error:
            status.setdefault('last_error', self._last_error)
        return status

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
            self._refreshing = True
            try:
                record_attempt = getattr(self.store, 'record_catalog_attempt', None)
                if callable(record_attempt):
                    record_attempt(self.source)
                entries = await fetch()
                if not isinstance(entries, list) or not entries:
                    raise ValueError('catalog contains no models')
                now = time.time()
                models = [annotate_catalog(m, self.source, checked_at=now, ttl_sec=self.ttl_sec)
                          for m in entries if isinstance(m, dict) and m.get('id')]
                if not models:
                    raise ValueError('catalog contains no valid model records')
                # This is a complete provider snapshot, unlike merged NVIDIA
                # sources. Retire absent rows only after validation succeeds.
                try:
                    self.store.upsert_catalog(models, source=self.source,
                                              ttl_sec=self.ttl_sec, retire_absent=True)
                except TypeError:
                    # Compatibility for small test doubles and older external
                    # stores; real ModelStateStore supports reconciliation.
                    self.store.upsert_catalog(models, source=self.source,
                                              ttl_sec=self.ttl_sec)
                record_success = getattr(self.store, 'record_catalog_success', None)
                if callable(record_success):
                    record_success(self.source, len(models))
                self.registry.register_catalog(models, revision='runtime-catalog')
                self._models = models
                self.client.schedule_catalog(self.store.provider, models, 'runtime-catalog')
                self._last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = {'code': type(exc).__name__, 'detail': None}
                record_failure = getattr(self.store, 'record_catalog_failure', None)
                if callable(record_failure):
                    try:
                        record_failure(self.source, type(exc).__name__)
                    except Exception:
                        logger.debug('catalog failure state write failed', exc_info=True)
                logger.warning('Catalog refresh failed (%s)', type(exc).__name__)
            finally:
                self._refreshing = False
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
