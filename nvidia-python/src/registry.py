#!/usr/bin/env python3
"""Cached exact-model NVIDIA catalog limits with provenance, never name guesses."""
import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

import aiohttp
from common.model.metadata import annotate_catalog, context_metadata, positive_int
from common.nvidia_model_specs import build_model_url, parse_build_context

logger = logging.getLogger('wrapper-nvidia')
NGC_FEATURED_URL = (os.environ.get('NGC_FEATURED_MODELS_URL') or
    'https://assets.ngc.nvidia.com/products/api-catalog/featured-models.json').rstrip('/')
REGISTRY_REFRESH_SEC = max(60, int(os.environ.get('REGISTRY_REFRESH_SEC', '3600')))
CACHE_FILE = os.environ.get('NGC_CONTEXT_CACHE_FILE') or str(
    Path(__file__).resolve().parents[1] / 'ngc-featured-cache.json')
# Kept as an import-compatible empty symbol: no unverified bootstrap limits.
STATIC_SEED = {}


class Registry:
    def __init__(self, *, enabled=True):
        self._enabled = enabled
        self._map = {}
        self._source = 'empty'
        self._last_sync = 0
        self._last_attempt = 0
        self._last_error: Optional[str] = None
        self._agent = None
        self._timer = None
        self._lock = asyncio.Lock()
        self._generation = 0
        self._build_attempts = {}
        self._model_ids = lambda: []
        self.on_refresh = None
        if enabled:
            self._load_cache_file()

    def set_external_agent(self, agent):
        self._agent = agent

    def set_model_ids(self, getter):
        self._model_ids = getter

    async def _fetch_live(self):
        timeout = aiohttp.ClientTimeout(total=20)
        session = self._agent or aiohttp.ClientSession(timeout=timeout)
        try:
            async with session.get(NGC_FEATURED_URL, headers={'Accept': 'application/json'},
                                   timeout=timeout) as resp:
                if resp.status != 200:
                    raise ValueError(f'NGC featured-models HTTP {resp.status}')
                body = await resp.json()
        finally:
            if self._agent is None:
                await session.close()
        arr = body.get('featured-models', body.get('models', body.get('data', []))) if isinstance(body, dict) else body
        if not isinstance(arr, list):
            raise ValueError('NGC catalog must contain a list')
        result = {}
        checked = time.time()
        for model in arr:
            if not isinstance(model, dict):
                continue
            mid = model.get('model') or model.get('id')
            if not isinstance(mid, str) or not mid:
                continue
            entry = annotate_catalog(model, NGC_FEATURED_URL, checked_at=checked,
                                     ttl_sec=REGISTRY_REFRESH_SEC, scope='nvidia_published_catalog')
            meta = entry['context_metadata']
            if not positive_int(meta.get('context_window')):
                continue
            result[mid] = {**entry, 'id': mid, 'context': meta['context_window']}
            if positive_int(meta.get('max_output_tokens')):
                result[mid]['maxOutput'] = meta['max_output_tokens']
        if not result:
            raise ValueError('NGC returned no usable context metadata')
        return result

    def _load_cache_file(self):
        try:
            raw = json.loads(Path(CACHE_FILE).read_text())
            # Previous caches could contain a static seed or invented output
            # cap. Re-fetch rather than promote these to authoritative facts.
            if not isinstance(raw, dict) or raw.get('schema') != 2 or raw.get('url') != NGC_FEATURED_URL:
                return
            entries = raw.get('map', {})
            if not isinstance(entries, dict):
                return
            self._map = {mid: value for mid, value in entries.items()
                         if isinstance(value, dict) and positive_int(value.get('context'))
                         and isinstance(value.get('context_metadata'), dict)
                         and value['context_metadata'].get('source') in
                         (NGC_FEATURED_URL, build_model_url(mid))}
            attempts = raw.get('build_attempts', {})
            self._build_attempts = {mid: stamp for mid, stamp in (attempts if isinstance(attempts, dict) else {}).items()
                                    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)}
            if self._map:
                self._source = 'cache'
                self._last_sync = float(raw.get('checked_at') or 0)
        except (OSError, ValueError, TypeError):
            pass

    def _save_cache_file(self):
        path = Path(CACHE_FILE)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as out:
                temporary = out.name
                json.dump({'schema': 2, 'url': NGC_FEATURED_URL,
                           'checked_at': self._last_sync, 'map': self._map,
                           'build_attempts': self._build_attempts}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            logger.warning('[registry] cache write failed: %s', type(exc).__name__)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    async def refresh(self, force=False):
        if not self._enabled:
            return {}
        started = time.time()
        generation = self._generation
        async with self._lock:
            if generation != self._generation or self._last_attempt >= started or (not force and self._last_attempt
                    and started - self._last_attempt < REGISTRY_REFRESH_SEC):
                return self.all()
            self._last_attempt = time.time()
            try:
                live = await self._fetch_live()
                # Keep missing entries at their original timestamp (stale),
                # never refresh an old value just because another model exists.
                self._map.update(live)
                self._source = 'live'
                self._last_sync = time.time()
                self._last_error = None
                self._save_cache_file()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                logger.warning('[registry] NGC refresh failed; retaining %d cached records', len(self._map))
            finally:
                self._generation += 1
            self._notify_refresh()
            return self.all()

    def _notify_refresh(self):
        if self.on_refresh is not None:
            try:
                self.on_refresh()
            except Exception as exc:
                logger.warning('[registry] catalog callback failed: %s', type(exc).__name__)

    async def refresh_build_metadata(self):
        if not self._enabled or os.environ.get('NVIDIA_BUILD_CONTEXT_ENABLED', 'true').lower() in ('0', 'false', 'no', 'off'):
            return
        ttl = max(3600, int(os.environ.get('NVIDIA_BUILD_CONTEXT_REFRESH_SEC', '86400')))
        semaphore = asyncio.Semaphore(max(1, min(8, int(os.environ.get('NVIDIA_BUILD_CONTEXT_CONCURRENCY', '3')))))
        now = time.time()
        models = []
        for mid in self._model_ids():
            existing = self._map.get(mid)
            if (existing and existing.get('context_metadata', {}).get('source') == NGC_FEATURED_URL
                    and not context_metadata(existing)['stale']):
                continue
            if build_model_url(mid) and now - self._build_attempts.get(mid, 0) >= ttl:
                models.append(mid)
        if not models:
            return
        session = self._agent or aiohttp.ClientSession()

        async def fetch(mid):
            async with semaphore:
                self._build_attempts[mid] = time.time()
                url = build_model_url(mid)
                try:
                    async with session.get(url, headers={'Accept': 'text/html'},
                            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
                        if response.status != 200:
                            return
                        raw = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            raw.extend(chunk)
                            if len(raw) > 4 * 1024 * 1024:
                                return
                        window = await asyncio.to_thread(parse_build_context,
                            raw.decode('utf-8', errors='replace'), mid)
                        if window is None:
                            return
                    entry = annotate_catalog({'id': mid, 'context': window}, url,
                        ttl_sec=ttl, scope='nvidia_published_model_specification')
                    # NGC may have gained an entry while this page was fetched.
                    current = context_metadata(self._map.get(mid))
                    if current.get('source') != NGC_FEATURED_URL or current['stale']:
                        self._map[mid] = entry
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass  # last good record and its original age remain intact
        try:
            await asyncio.gather(*(fetch(mid) for mid in models))
        finally:
            if self._agent is None:
                await session.close()
            self._save_cache_file()
        self._notify_refresh()

    def start(self):
        if not self._enabled or (self._timer and not self._timer.done()):
            return
        async def tick():
            while True:
                try:
                    await self.refresh_build_metadata()
                except Exception as exc:
                    logger.warning('[registry] background enrichment failed: %s', type(exc).__name__)
                await asyncio.sleep(REGISTRY_REFRESH_SEC)
                await self.refresh()
        self._timer = asyncio.create_task(tick())

    def stop(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def close(self):
        task = self._timer
        self.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    def get_official_context(self, model_id):
        # Exact ID only: org-a/foo and org-b/foo need not share a capacity.
        value = copy.deepcopy(self._map.get(model_id))
        if value is not None:
            value['context_metadata'] = context_metadata(value)
        return value

    def has_official_context(self, model_id):
        return model_id in self._map

    def all(self):
        return {mid: self.get_official_context(mid) for mid in self._map}

    def status(self):
        return {'enabled': self._enabled, 'source': self._source, 'models': len(self._map),
                'lastSync': self._last_sync or None, 'lastAttempt': self._last_attempt or None,
                'lastError': self._last_error, 'url': NGC_FEATURED_URL,
                'staleModels': sum(context_metadata(value)['stale'] for value in self._map.values()),
                'buildCheckedModels': len(self._build_attempts),
                'buildKnownModels': sum(value.get('context_metadata', {}).get('scope') ==
                    'nvidia_published_model_specification' for value in self._map.values())}
